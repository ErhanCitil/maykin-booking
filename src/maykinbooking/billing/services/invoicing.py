"""
Invoices and credit notes.

An invoice is issued when a booking is confirmed (at booking time for payment at
the hotel, after the payment for online payment); a cancelled booking with an
invoice gets a credit note. Number, amounts and lines are fixed in one
transaction, so a rollback leaves no gap in the numbering. The PDF is stored once
after the commit; if it is missing later, it is rendered again.

Prices include VAT. The VAT is computed back over the total of the invoice:
``excl = incl / (1 + rate)`` rounded to cents, ``vat = incl - excl``. The rate
follows from the country of the hotel and the check-in date.

This module never imports the booking service.
"""
import hashlib
import logging
import secrets
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Optional

from django.conf import settings
from django.core.files.base import ContentFile
from django.utils import timezone, translation
from django.utils.formats import date_format
from django.utils.translation import gettext

from maykinbooking.base.models import OrderStatus, PaymentStatus
from maykinbooking.utils.transactions import on_commit_safely

from ..models import (
    Invoice,
    InvoiceKind,
    InvoiceLine,
    InvoiceLineKind,
    InvoiceSequence,
    PaymentState,
    VatRate,
)
from ..pdf import render_invoice_pdf
from ..storage import get_invoice_storage
from .pricing import CENT, ZERO, discount_lines, money, surcharge_labels

logger = logging.getLogger(__name__)

HUNDRED = Decimal("100")


@dataclass(frozen=True)
class SellerDetails:
    name: str
    address: str
    kvk: str
    vat_id: str

    @property
    def complete(self):
        return all([self.name, self.address, self.kvk, self.vat_id])


def seller_details() -> SellerDetails:
    """
    The seller on the invoices, from the ``INVOICE_SELLER_*`` settings. A ``\\n``
    in ``INVOICE_SELLER_ADDRESS`` starts a new line.
    """
    return SellerDetails(
        name=settings.INVOICE_SELLER_NAME.strip(),
        address=settings.INVOICE_SELLER_ADDRESS.replace("\\n", "\n").strip(),
        kvk=settings.INVOICE_SELLER_KVK.strip(),
        vat_id=settings.INVOICE_SELLER_VAT_ID.strip(),
    )


def split_vat(incl, rate_percent):
    """``(excl, vat)`` of an amount including VAT at ``rate_percent``."""
    incl = Decimal(incl)
    excl = money(incl / (1 + Decimal(rate_percent) / HUNDRED))
    return excl, incl - excl


def vat_rate_for(country, on_date) -> Optional[Decimal]:
    """The VAT rate of ``country`` on ``on_date``, or ``None`` if there is none."""
    return (
        VatRate.objects.filter(country=str(country), valid_from__lte=on_date)
        .order_by("-valid_from")
        .values_list("rate", flat=True)
        .first()
    )


def _excl(amount, rate):
    return amount if rate is None else split_vat(amount, rate)[0]


def _next_number(year):
    """
    Take the next number of ``year``. Locks the counter of the year until the
    transaction ends, so the numbers have no gaps, also when invoices are issued
    at the same time. Call it as late as possible in the transaction.
    """
    InvoiceSequence.objects.get_or_create(year=year)
    sequence = InvoiceSequence.objects.select_for_update().get(year=year)
    sequence.last_number += 1
    sequence.save(update_fields=["last_number"])
    return sequence.last_number


def _night_description(night):
    description = gettext("Overnachting %(date)s") % {
        "date": date_format(night.date, "l j F Y")
    }
    surcharges = surcharge_labels(night)
    if surcharges:
        description += f" ({', '.join(surcharges)})"
    return description[:255]


def _payment_note(order):
    if order.payment_status == PaymentStatus.PAID:
        payment = (
            order.payments.filter(status=PaymentState.PAID)
            .order_by("paid_at", "pk")
            .first()
        )
        paid_at = (payment and payment.paid_at) or timezone.now()
        return gettext("Betaald via iDEAL op %(date)s.") % {
            "date": date_format(timezone.localtime(paid_at).date(), "j F Y")
        }
    if order.total_price <= 0:
        return gettext("Er hoeft niets betaald te worden.")
    if order.payment_status == PaymentStatus.AT_HOTEL:
        return gettext("Te betalen in het hotel.")
    return ""


def _spread_revenue(night_lines, total_excl):
    """
    Give every night line its share of ``total_excl`` (0 or more), pro rata to
    its price. The shares are rounded down to cents and the cents that are left
    go to the nights with the largest rounding loss (the earliest first), so
    the shares add up exactly and none is negative.
    """
    if not night_lines:
        return
    subtotal = sum((line.amount_incl for line in night_lines), ZERO)
    if subtotal <= 0 or total_excl <= 0:
        for line in night_lines:
            line.revenue_excl = ZERO
        night_lines[-1].revenue_excl = total_excl
        return
    exact = [total_excl * line.amount_incl / subtotal for line in night_lines]
    shares = [share.quantize(CENT, rounding=ROUND_DOWN) for share in exact]
    cents_left = int((total_excl - sum(shares, ZERO)) / CENT)
    by_loss = sorted(
        range(len(night_lines)), key=lambda index: (shares[index] - exact[index], index)
    )
    for index in by_loss[:cents_left]:
        shares[index] += CENT
    for line, share in zip(night_lines, shares):
        line.revenue_excl = share


def _invoice_lines(order, rate):
    nights = [
        InvoiceLine(
            kind=InvoiceLineKind.NIGHT,
            date=night.date,
            description=_night_description(night),
            amount_incl=night.price,
            amount_excl=_excl(night.price, rate),
        )
        for night in order.nights.order_by("date")
    ]
    discounts = [
        InvoiceLine(
            kind=InvoiceLineKind.DISCOUNT,
            description=line["label"][:255],
            amount_incl=-line["amount"],
            amount_excl=-_excl(line["amount"], rate),
            revenue_excl=ZERO,
        )
        for line in discount_lines(order)
    ]
    return nights, discounts


def _customer_address(order):
    country = order.country.name if order.country else ""
    place = f"{order.zipcode} {order.city}".strip()
    return "\n".join(part for part in (order.address, place, country) if part)


def issue_invoice(order, *, actor=None, today=None) -> Invoice:
    """
    The invoice of a confirmed ``order``; issued now if it has none yet.

    The caller has locked the order (``select_for_update``) in a transaction.
    The PDF is stored after the commit (:func:`store_invoice_pdf`).
    """
    existing = order.invoices.filter(kind=InvoiceKind.INVOICE).first()
    if existing is not None:
        return existing
    if order.status != OrderStatus.CONFIRMED:
        raise ValueError(f"Order {order.pk} is not confirmed: no invoice.")

    today = today or timezone.localdate()
    hotel = order.hotel
    country = str(hotel.country or "")
    rate = vat_rate_for(country, order.start_date)
    seller = seller_details()
    total_incl = order.total_price
    if rate is None:
        total_excl, vat_amount = total_incl, ZERO
    else:
        total_excl, vat_amount = split_vat(total_incl, rate)

    with translation.override(order.language or settings.LANGUAGE_CODE):
        night_lines, discount_lines = _invoice_lines(order, rate)
        payment_note = _payment_note(order)
        customer_address = _customer_address(order)
        room_type = order.room.get_room_type_display()
    _spread_revenue(night_lines, total_excl)

    sequence = _next_number(today.year)
    invoice = Invoice.objects.create(
        number=f"{today.year}-{sequence:05d}",
        year=today.year,
        sequence=sequence,
        kind=InvoiceKind.INVOICE,
        order=order,
        issue_date=today,
        seller_name=seller.name,
        seller_address=seller.address,
        seller_kvk=seller.kvk,
        seller_vat_id=seller.vat_id,
        customer_name=f"{order.first_name} {order.last_name}".strip(),
        customer_email=order.email,
        customer_address=customer_address,
        hotel_name=hotel.name,
        hotel_address=", ".join(
            part for part in (hotel.address, hotel.city.name) if part
        )[: Invoice._meta.get_field("hotel_address").max_length],
        room_type=room_type,
        start_date=order.start_date,
        end_date=order.end_date,
        nights=order.night_count,
        vat_country=country,
        vat_rate=rate,
        total_excl=total_excl,
        vat_amount=vat_amount,
        total_incl=total_incl,
        payment_note=payment_note[:255],
        is_test=not seller.complete or rate is None,
        created_by=actor,
    )
    lines = night_lines + discount_lines
    for position, line in enumerate(lines, start=1):
        line.invoice = invoice
        line.position = position
    InvoiceLine.objects.bulk_create(lines)
    on_commit_safely(store_invoice_pdf, invoice.pk)
    return invoice


def issue_credit_note(invoice, *, actor=None, today=None) -> Invoice:
    """
    The credit note of ``invoice``; issued now if it has none yet. All of the
    invoice is credited: the same snapshot (seller, customer, rate, test
    watermark) with negative amounts. Same numbering as the invoices.

    The caller has locked the order in a transaction.
    """
    if invoice.kind != InvoiceKind.INVOICE:
        raise ValueError(f"{invoice.number} is not an invoice.")
    existing = invoice.credit_notes.first()
    if existing is not None:
        return existing

    today = today or timezone.localdate()
    order = invoice.order
    with translation.override(order.language or settings.LANGUAGE_CODE):
        if order.payment_status == PaymentStatus.PAID:
            payment_note = gettext("Het betaalde bedrag wordt teruggestort.")
        else:
            payment_note = gettext("Er hoeft niets betaald te worden.")
    lines = [
        InvoiceLine(
            kind=line.kind,
            date=line.date,
            description=line.description,
            amount_incl=-line.amount_incl,
            amount_excl=-line.amount_excl,
            revenue_excl=-line.revenue_excl,
        )
        for line in invoice.lines.order_by("position")
    ]

    sequence = _next_number(today.year)
    snapshot_fields = (
        "seller_name",
        "seller_address",
        "seller_kvk",
        "seller_vat_id",
        "customer_name",
        "customer_email",
        "customer_address",
        "hotel_name",
        "hotel_address",
        "room_type",
        "start_date",
        "end_date",
        "nights",
        "vat_country",
        "vat_rate",
        "is_test",
    )
    credit_note = Invoice.objects.create(
        number=f"{today.year}-{sequence:05d}",
        year=today.year,
        sequence=sequence,
        kind=InvoiceKind.CREDIT_NOTE,
        order=order,
        credited_invoice=invoice,
        issue_date=today,
        total_excl=-invoice.total_excl,
        vat_amount=-invoice.vat_amount,
        total_incl=-invoice.total_incl,
        payment_note=payment_note[:255],
        created_by=actor,
        **{field: getattr(invoice, field) for field in snapshot_fields},
    )
    for position, line in enumerate(lines, start=1):
        line.invoice = credit_note
        line.position = position
    InvoiceLine.objects.bulk_create(lines)
    on_commit_safely(store_invoice_pdf, credit_note.pk)
    return credit_note


def _pdf_name(invoice):
    # The random part makes the name impossible to guess.
    return f"{invoice.year}/{invoice.number}-{secrets.token_hex(8)}.pdf"


def store_invoice_pdf(invoice_id):
    """
    Render the PDF of an invoice and store it, if it has none yet. Runs after
    the commit that issued the invoice; never raises (a failure is logged, the
    PDF is rendered when it is asked for).
    """
    try:
        invoice = Invoice.objects.select_related("order", "credited_invoice").get(
            pk=invoice_id
        )
        if invoice.pdf:
            return
        _store_pdf(invoice)
    except Exception:
        logger.exception("Storing the PDF of invoice %s failed", invoice_id)


def _store_pdf(invoice):
    """Render, store and attach the PDF; returns the bytes."""
    content = render_invoice_pdf(invoice, list(invoice.lines.order_by("position")))
    storage = get_invoice_storage()
    name = storage.save(_pdf_name(invoice), ContentFile(content))
    checksum = hashlib.sha256(content).hexdigest()
    if not Invoice.objects._attach_pdf(invoice.pk, name, checksum):
        # Another process stored one first: keep that one.
        storage.delete(name)
    return content


def invoice_pdf_bytes(invoice) -> bytes:
    """
    The PDF of an invoice: the stored file, or (if it was never stored) render
    and store it now. If the stored file is missing or changed, it is rendered
    again from the snapshot and the problem is logged.
    """
    if not invoice.pdf:
        try:
            return _store_pdf(invoice)
        except OSError:
            # The guest still gets the invoice, only it is not stored yet.
            logger.exception("Storing the PDF of invoice %s failed", invoice.pk)
            return render_invoice_pdf(invoice, list(invoice.lines.order_by("position")))
    try:
        with get_invoice_storage().open(invoice.pdf.name, "rb") as file:
            content = file.read()
    except OSError:
        logger.error("The PDF of invoice %s is missing; rendered again", invoice.pk)
        return render_invoice_pdf(invoice, list(invoice.lines.order_by("position")))
    if invoice.pdf_sha256 and hashlib.sha256(content).hexdigest() != invoice.pdf_sha256:
        logger.error("The PDF of invoice %s was changed; rendered again", invoice.pk)
        return render_invoice_pdf(invoice, list(invoice.lines.order_by("position")))
    return content


def invoice_filename(invoice):
    """``factuur_<number>.pdf`` or ``creditnota_<number>.pdf``."""
    return invoice.filename
