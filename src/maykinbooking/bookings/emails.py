"""
Mails to the guest about a booking.

Every mail gets an ``EmailLog`` row (without the body). The templates are in
``templates/emails/``: ``<kind>_subject.txt``, ``<kind>.txt`` and ``<kind>.html``.

Attachments: the confirmation has the booking PDF and the invoice (orders from
before invoicing have no invoice), the cancellation has the credit note.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone, translation

from maykinbooking.base.models import EmailKind, EmailLog, EmailStatus, Order
from maykinbooking.billing.models import CLOSED_REFUND_STATES, InvoiceKind, Refund
from maykinbooking.billing.services.invoicing import invoice_pdf_bytes
from maykinbooking.billing.services.payments import confirming_payment
from maykinbooking.billing.services.pricing import discount_lines, night_lines

from .pdf import render_order_pdf
from .services.booking import can_guest_cancel, guest_cancellation_deadline

logger = logging.getLogger(__name__)

# EmailLog.error keeps the start of the error; the full traceback is in the log.
MAX_ERROR_LENGTH = 500

TEMPLATES = {
    EmailKind.BOOKING_CONFIRMATION: "emails/booking_confirmation",
    EmailKind.BOOKING_CANCELLATION: "emails/booking_cancellation",
    EmailKind.PAYMENT_REFUND: "emails/payment_refund",
}


def my_booking_url(order):
    """
    Absolute URL of the "Mijn boeking" page. Built with ``SITE_URL``, never with
    the Host header of a request.
    """
    path = reverse("bookings:detail", kwargs={"token": order.token})
    return settings.SITE_URL.rstrip("/") + path


def last_free_cancellation_day(deadline):
    """
    The last date on which a guest can cancel before ``deadline``, in the time
    zone of the hotels (``TIME_ZONE``), whatever time zone is active.
    """
    local = timezone.localtime(deadline, timezone.get_default_timezone())
    return local.date() - timedelta(days=1)


def booking_context(order):
    """
    Template context for the mails and the PDF of an order (and "Mijn
    boeking"). Money:

    * ``invoice`` / ``credit_note``: the ``Invoice`` of the order and its credit
      note, or ``None`` (orders from before invoicing have neither);
    * ``payment``: the online payment that paid the order (else the last one),
      or ``None`` for payment at the hotel;
    * ``refund``: the last refund that was not refused by the provider, or
      ``None``; ``refund.status`` tells whether the money is back yet;
    * ``discount_lines`` (``billing.services.pricing.discount_lines``) and
      ``subtotal``: the price of the nights before the discounts;
    * ``night_lines``: the nights with their surcharge labels
      (``billing.services.pricing.night_lines``), the same as everywhere.
    """
    deadline = guest_cancellation_deadline(order)
    invoices = {invoice.kind: invoice for invoice in order.invoices.all()}
    payments = list(order.payments.order_by("created_at", "pk"))
    nights = list(order.nights.all())
    refund = (
        Refund.objects.filter(payment__order=order)
        .exclude(status__in=CLOSED_REFUND_STATES)
        .order_by("-created_at", "-pk")
        .first()
    )
    return {
        "order": order,
        "nights": nights,
        "night_lines": night_lines(nights),
        "night_count": order.night_count,
        "my_booking_url": my_booking_url(order),
        "cancel_deadline": deadline,
        # The deadline is 00:00 at the start of a day, so the last day on which a
        # guest can still cancel for free is the day before. Show this to guests
        # ("t/m vrijdag 4 december") instead of "tot zaterdag 00:00".
        "last_free_cancellation_day": last_free_cancellation_day(deadline),
        "can_cancel": can_guest_cancel(order),
        "contact_email": settings.BOOKING_CONTACT_EMAIL,
        "invoice": invoices.get(InvoiceKind.INVOICE),
        "credit_note": invoices.get(InvoiceKind.CREDIT_NOTE),
        "payment": confirming_payment(order) or (payments[-1] if payments else None),
        "refund": refund,
        "discount_lines": discount_lines(order),
        "subtotal": order.subtotal_price,
    }


def _attachments(order, kind, context):
    """``[(filename, content)]`` of the PDFs that go with a mail."""
    if kind == EmailKind.BOOKING_CONFIRMATION:
        attachments = [(f"boeking_{order.pk}.pdf", render_order_pdf(order))]
        invoice = context["invoice"]
        if invoice is not None:
            attachments.append((invoice.filename, invoice_pdf_bytes(invoice)))
        return attachments
    if kind == EmailKind.BOOKING_CANCELLATION:
        credit_note = context["credit_note"]
        if credit_note is not None:
            return [(credit_note.filename, invoice_pdf_bytes(credit_note))]
    return []


def _build_message(order, kind, *, refund_id=None):
    template = TEMPLATES[kind]
    with translation.override(order.language or settings.LANGUAGE_CODE):
        context = booking_context(order)
        if refund_id is not None:
            # The mail is about this refund (an order can have more than one).
            context["refund"] = Refund.objects.get(pk=refund_id, payment__order=order)
        # A subject is one line: drop the newlines and indentation of the template.
        subject = " ".join(render_to_string(f"{template}_subject.txt", context).split())
        text = render_to_string(f"{template}.txt", context)
        html = render_to_string(f"{template}.html", context)
        attachments = _attachments(order, kind, context)

    message = EmailMultiAlternatives(
        subject=subject,
        body=text,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[order.email],
        reply_to=[settings.BOOKING_CONTACT_EMAIL],
    )
    message.attach_alternative(html, "text/html")
    for filename, content in attachments:
        message.attach(filename, content, "application/pdf")
    return message


def send_booking_email(order_id, kind, *, triggered_by=None, refund_id=None):
    """
    Send a mail of ``kind`` (an ``EmailKind``) about an order and log it.

    Never raises: it runs in ``transaction.on_commit`` after a booking, where an
    exception would turn a successful booking into an error page. A failure is
    logged (``logger.exception``) and stored in the ``EmailLog`` with status
    ``failed``. Returns the ``EmailLog``, or ``None`` if not even that could be
    created (unknown order or kind).

    ``triggered_by``: the staff member who (re)sent the mail, if any.
    ``refund_id``: for ``PAYMENT_REFUND``, the refund the mail is about (by
    default the last one of the order).
    """
    if kind not in TEMPLATES:
        logger.error("Unknown booking mail %r for order %s", kind, order_id)
        return None
    # The EmailLog writes each get their own (inner) transaction, so a database
    # error here cannot break a transaction of the caller.
    try:
        order = Order.objects.select_related("hotel__city", "room").get(pk=order_id)
        with transaction.atomic():
            log = EmailLog.objects.create(
                order=order,
                kind=kind,
                recipient=order.email,
                status=EmailStatus.PENDING,
                triggered_by=triggered_by,
            )
    except Exception:
        logger.exception("Could not start the %s mail of order %s", kind, order_id)
        return None

    try:
        message = _build_message(order, kind, refund_id=refund_id)
        log.subject = message.subject[:255]
        message.send()
    except Exception as exc:
        logger.exception("Sending the %s mail of order %s failed", kind, order_id)
        log.status = EmailStatus.FAILED
        log.error = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_LENGTH]
    else:
        log.status = EmailStatus.SENT
        log.sent_at = timezone.now()

    try:
        with transaction.atomic():
            log.save(update_fields=["subject", "status", "error", "sent_at"])
    except Exception:
        logger.exception("Could not update EmailLog %s", log.pk)
    return log


__all__ = [
    "booking_context",
    # Kept here for the dashboard; it lives in billing.services.pricing.
    "discount_lines",
    "last_free_cancellation_day",
    "my_booking_url",
    "send_booking_email",
]
