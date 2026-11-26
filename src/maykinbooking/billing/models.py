"""
Price rules, discount codes, payments, refunds and invoices.

The money fields of an order (total, discounts) and of its nights (price
breakdown) are snapshots on ``base.Order`` and ``base.OrderNight``. The models
here refer to orders; ``base`` never refers to billing.

Issued invoices are immutable: see :class:`Invoice`.
"""
import uuid
from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.db.models import F, Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from django_countries.fields import CountryField

from maykinbooking.base.models import Order

from .storage import get_invoice_storage

ZERO = Decimal("0.00")

# A discount code as stored: upper case letters, digits and hyphens.
DISCOUNT_CODE_PATTERN = r"^[A-Z0-9-]{3,40}$"


def normalize_discount_code(code):
    """A discount code as typed (``" zomer-26 "``) as stored (``"ZOMER-26"``)."""
    return (code or "").strip().upper()


class PricingSettings(models.Model):
    """
    The price rules of the whole site. There is one row (pk=1): use
    :meth:`load`. All percentages are 0 until staff fills them in.
    """

    weekend_percent = models.DecimalField(
        _("weekendtoeslag (%)"),
        max_digits=5,
        decimal_places=2,
        default=ZERO,
        validators=[MinValueValidator(0), MaxValueValidator(200)],
        help_text=_("Voor de vrijdag- en zaterdagnacht."),
    )
    long_stay_min_nights = models.PositiveSmallIntegerField(
        _("lang verblijf vanaf (nachten)"),
        default=7,
        validators=[MinValueValidator(2), MaxValueValidator(30)],
    )
    long_stay_percent = models.DecimalField(
        _("korting lang verblijf (%)"),
        max_digits=5,
        decimal_places=2,
        default=ZERO,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text=_("Over het subtotaal van het verblijf."),
    )
    updated_at = models.DateTimeField(_("gewijzigd op"), auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("gewijzigd door"),
        related_name="+",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = _("prijsregels")
        verbose_name_plural = _("prijsregels")
        constraints = [
            models.CheckConstraint(
                check=Q(id=1), name="billing_pricingsettings_singleton"
            ),
        ]

    def __str__(self):
        return str(self._meta.verbose_name)

    @classmethod
    def load(cls):
        """The price rules; the row is created (all 0) when it is missing."""
        rules, _created = cls.objects.get_or_create(pk=1)
        return rules


class SeasonPeriod(models.Model):
    """
    A high season: every night from ``start_date`` up to and including
    ``end_date`` gets ``percent`` extra. Periods do not overlap; that is checked
    by ``billing.services.pricing.save_season``.
    """

    name = models.CharField(_("naam"), max_length=100)
    start_date = models.DateField(_("van"))
    end_date = models.DateField(_("tot en met"))
    percent = models.DecimalField(
        _("toeslag (%)"),
        max_digits=5,
        decimal_places=2,
        validators=[MinValueValidator(0), MaxValueValidator(200)],
    )

    class Meta:
        verbose_name = _("seizoen")
        verbose_name_plural = _("seizoenen")
        ordering = ("start_date", "id")
        constraints = [
            models.CheckConstraint(
                check=Q(end_date__gte=F("start_date")),
                name="billing_seasonperiod_end_not_before_start",
            ),
            models.CheckConstraint(
                check=Q(percent__gte=0), name="billing_seasonperiod_percent_gte_0"
            ),
        ]
        indexes = [models.Index(fields=["start_date"], name="billing_season_start_idx")]

    def __str__(self):
        return f"{self.name} ({self.start_date} - {self.end_date})"


class DiscountKind(models.TextChoices):
    PERCENT = "percent", _("Percentage")
    FIXED = "fixed", _("Vast bedrag")


class DiscountCode(models.Model):
    """
    A code the guest enters in the checkout. ``times_used`` counts the orders
    that use it; it is only changed with a single ``UPDATE`` (see
    ``billing.services.pricing.redeem_discount_code``), so two guests cannot use
    the last one at the same time.
    """

    code = models.CharField(
        _("code"),
        max_length=40,
        unique=True,
        validators=[
            RegexValidator(
                DISCOUNT_CODE_PATTERN,
                _("Gebruik 3 tot 40 hoofdletters, cijfers of streepjes."),
            )
        ],
    )
    kind = models.CharField(_("soort"), max_length=10, choices=DiscountKind.choices)
    value = models.DecimalField(
        _("waarde"),
        max_digits=8,
        decimal_places=2,
        help_text=_("Een percentage (1-100) of een bedrag in euro."),
    )
    valid_from = models.DateField(_("geldig van"))
    valid_until = models.DateField(_("geldig tot en met"))
    max_uses = models.PositiveIntegerField(
        _("maximaal aantal keer"),
        null=True,
        blank=True,
        help_text=_("Leeg is onbeperkt."),
    )
    times_used = models.PositiveIntegerField(_("aantal keer gebruikt"), default=0)
    is_active = models.BooleanField(_("actief"), default=True)
    description = models.CharField(_("omschrijving"), max_length=200, blank=True)
    created_at = models.DateTimeField(_("aangemaakt op"), default=timezone.now)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("aangemaakt door"),
        related_name="+",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = _("kortingscode")
        verbose_name_plural = _("kortingscodes")
        ordering = ("code",)
        constraints = [
            models.CheckConstraint(
                check=Q(value__gt=0), name="billing_discountcode_value_gt_0"
            ),
            models.CheckConstraint(
                check=~Q(kind=DiscountKind.PERCENT) | Q(value__lte=100),
                name="billing_discountcode_percent_lte_100",
            ),
            models.CheckConstraint(
                check=Q(valid_until__gte=F("valid_from")),
                name="billing_discountcode_until_not_before_from",
            ),
            models.CheckConstraint(
                check=Q(max_uses__isnull=True) | Q(times_used__lte=F("max_uses")),
                name="billing_discountcode_times_used_lte_max_uses",
            ),
        ]

    def __str__(self):
        return self.code

    def save(self, *args, **kwargs):
        self.code = normalize_discount_code(self.code)
        super().save(*args, **kwargs)


class DiscountRedemption(models.Model):
    """
    An order that used a discount code. ``released_at`` is set when the order
    expired: the use no longer counts. A cancelled order keeps counting.
    """

    code = models.ForeignKey(
        DiscountCode,
        verbose_name=_("kortingscode"),
        related_name="redemptions",
        on_delete=models.PROTECT,
    )
    order = models.OneToOneField(
        Order,
        verbose_name=_("boeking"),
        related_name="discount_redemption",
        on_delete=models.PROTECT,
    )
    amount = models.DecimalField(_("korting"), max_digits=10, decimal_places=2)
    created_at = models.DateTimeField(_("gebruikt op"), default=timezone.now)
    released_at = models.DateTimeField(_("vrijgegeven op"), null=True, blank=True)

    class Meta:
        verbose_name = _("gebruik van kortingscode")
        verbose_name_plural = _("gebruik van kortingscodes")
        indexes = [
            models.Index(
                fields=["code", "released_at"], name="billing_redemption_code_idx"
            )
        ]

    def __str__(self):
        return f"{self.code_id}: {self.order_id}"


class ProviderName(models.TextChoices):
    MOLLIE = "mollie", "Mollie"
    FAKE = "fake", _("Nep-provider")


class PaymentState(models.TextChoices):
    """The status of a payment at the provider (Mollie's names), plus ``error``."""

    CREATED = "created", _("Aangemaakt")
    OPEN = "open", _("Open")
    PENDING = "pending", _("In behandeling")
    AUTHORIZED = "authorized", _("Geautoriseerd")
    PAID = "paid", _("Betaald")
    CANCELED = "canceled", _("Geannuleerd")
    EXPIRED = "expired", _("Verlopen")
    FAILED = "failed", _("Mislukt")
    # Ours: the payment could not be started (or its amount did not match).
    ERROR = "error", _("Fout")


# The guest can still pay.
OPEN_PAYMENT_STATES = (
    PaymentState.CREATED,
    PaymentState.OPEN,
    PaymentState.PENDING,
    PaymentState.AUTHORIZED,
)
# Final without money: the order can expire.
FAILED_PAYMENT_STATES = (
    PaymentState.CANCELED,
    PaymentState.EXPIRED,
    PaymentState.FAILED,
)


class Payment(models.Model):
    """
    One online payment attempt of an order.

    ``reference`` is ours: the idempotency key for the provider and the part of
    the return URL. ``processed_at`` is set once a final status (paid, failed,
    canceled, expired) has been applied to the order, so a webhook that comes
    three times confirms once.
    """

    order = models.ForeignKey(
        Order,
        verbose_name=_("boeking"),
        related_name="payments",
        on_delete=models.PROTECT,
    )
    reference = models.UUIDField(
        _("referentie"), default=uuid.uuid4, unique=True, editable=False
    )
    provider = models.CharField(
        _("provider"), max_length=10, choices=ProviderName.choices
    )
    provider_id = models.CharField(_("id bij de provider"), max_length=64, blank=True)
    status = models.CharField(
        _("status"),
        max_length=20,
        choices=PaymentState.choices,
        default=PaymentState.CREATED,
    )
    amount = models.DecimalField(_("bedrag"), max_digits=10, decimal_places=2)
    currency = models.CharField(_("valuta"), max_length=3, default="EUR")
    method = models.CharField(_("betaalmethode"), max_length=30, blank=True)
    checkout_url = models.URLField(_("betaalpagina"), max_length=500, blank=True)
    paid_at = models.DateTimeField(_("betaald op"), null=True, blank=True)
    processed_at = models.DateTimeField(_("verwerkt op"), null=True, blank=True)
    last_error = models.TextField(_("laatste fout"), blank=True)
    created_at = models.DateTimeField(_("aangemaakt op"), default=timezone.now)
    updated_at = models.DateTimeField(_("gewijzigd op"), auto_now=True)

    class Meta:
        verbose_name = _("betaling")
        verbose_name_plural = _("betalingen")
        ordering = ("created_at", "id")
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "provider_id"],
                condition=~Q(provider_id=""),
                name="billing_payment_unique_provider_id",
            ),
            models.CheckConstraint(
                check=Q(amount__gt=0), name="billing_payment_amount_gt_0"
            ),
        ]
        indexes = [
            models.Index(
                fields=["status", "created_at"], name="billing_payment_status_idx"
            )
        ]

    def __str__(self):
        return f"{self.order_id}: {self.provider_id or self.reference} ({self.status})"

    @property
    def is_open(self):
        return self.status in OPEN_PAYMENT_STATES


class RefundState(models.TextChoices):
    """Mollie's refund statuses, plus ``requested`` and ``error`` (ours)."""

    REQUESTED = "requested", _("Aangevraagd")
    QUEUED = "queued", _("In de wachtrij")
    PENDING = "pending", _("In behandeling")
    PROCESSING = "processing", _("Wordt verwerkt")
    REFUNDED = "refunded", _("Terugbetaald")
    FAILED = "failed", _("Mislukt")
    CANCELED = "canceled", _("Geannuleerd")
    # Ours: sending it to the provider failed; it is tried again.
    ERROR = "error", _("Fout")


# Sent to the provider, waiting for the money to go back.
IN_PROGRESS_REFUND_STATES = (
    RefundState.QUEUED,
    RefundState.PENDING,
    RefundState.PROCESSING,
)
# A refund in one of these states no longer counts: a new one can be made.
CLOSED_REFUND_STATES = (RefundState.FAILED, RefundState.CANCELED)


class RefundReason(models.TextChoices):
    CANCELLATION = "cancellation", _("Annulering")
    LATE_PAYMENT = "late_payment", _("Betaling na verlopen reservering")
    DUPLICATE = "duplicate", _("Dubbele betaling")
    # Paid at the provider, but not the amount (or currency) of the payment.
    MISMATCH = "mismatch", _("Afwijkend bedrag")


class Refund(models.Model):
    """
    Money that goes back to the guest: always the full amount of a payment.

    The row is made in the transaction that cancels (or expires) the order; it is
    sent to the provider after the commit (``billing.services.payments``).
    """

    payment = models.ForeignKey(
        Payment,
        verbose_name=_("betaling"),
        related_name="refunds",
        on_delete=models.PROTECT,
    )
    reference = models.UUIDField(
        _("referentie"), default=uuid.uuid4, unique=True, editable=False
    )
    provider_id = models.CharField(_("id bij de provider"), max_length=64, blank=True)
    amount = models.DecimalField(_("bedrag"), max_digits=10, decimal_places=2)
    currency = models.CharField(_("valuta"), max_length=3, default="EUR")
    status = models.CharField(
        _("status"),
        max_length=20,
        choices=RefundState.choices,
        default=RefundState.REQUESTED,
    )
    reason = models.CharField(_("reden"), max_length=20, choices=RefundReason.choices)
    attempts = models.PositiveSmallIntegerField(_("pogingen"), default=0)
    error = models.TextField(_("foutmelding"), blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("aangevraagd door"),
        related_name="+",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(_("aangemaakt op"), default=timezone.now)
    updated_at = models.DateTimeField(_("gewijzigd op"), auto_now=True)
    refunded_at = models.DateTimeField(_("terugbetaald op"), null=True, blank=True)

    class Meta:
        verbose_name = _("terugbetaling")
        verbose_name_plural = _("terugbetalingen")
        ordering = ("created_at", "id")
        constraints = [
            models.UniqueConstraint(
                fields=["payment"],
                condition=~Q(status__in=CLOSED_REFUND_STATES),
                name="billing_refund_one_open_per_payment",
            ),
            models.CheckConstraint(
                check=Q(amount__gt=0), name="billing_refund_amount_gt_0"
            ),
        ]

    def __str__(self):
        return f"{self.payment_id}: {self.amount} ({self.status})"


class InvoiceSequence(models.Model):
    """The last invoice number of a year. Locked while a number is taken."""

    year = models.PositiveSmallIntegerField(_("jaar"), primary_key=True)
    last_number = models.PositiveIntegerField(_("laatste nummer"), default=0)

    class Meta:
        verbose_name = _("factuurnummering")
        verbose_name_plural = _("factuurnummering")

    def __str__(self):
        return f"{self.year}: {self.last_number}"


class ImmutableInvoiceError(Exception):
    """An issued invoice (or one of its lines) cannot be changed or deleted."""


class ImmutableQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ImmutableInvoiceError("Issued invoices cannot be changed.")

    update.alters_data = True

    def bulk_update(self, objs, fields, batch_size=None):
        raise ImmutableInvoiceError("Issued invoices cannot be changed.")

    bulk_update.alters_data = True

    def delete(self):
        raise ImmutableInvoiceError("Issued invoices cannot be deleted.")

    delete.alters_data = True
    delete.queryset_only = True

    def _update(self, values):
        # What Model.save uses to update a row, also for fixtures (loaddata).
        raise ImmutableInvoiceError("Issued invoices cannot be changed.")

    _update.alters_data = True


class InvoiceManager(models.Manager.from_queryset(ImmutableQuerySet)):
    def _attach_pdf(self, pk, name, sha256):
        """
        The only change ever made to an invoice: store the name and checksum of
        its PDF, once. Returns ``False`` if the invoice already has a PDF.
        """
        unfinished = self.get_queryset().filter(pk=pk, pdf="")
        return models.QuerySet.update(unfinished, pdf=name, pdf_sha256=sha256) == 1


class InvoiceLineManager(models.Manager.from_queryset(ImmutableQuerySet)):
    pass


class ImmutableModel(models.Model):
    """
    Can be inserted, never updated or deleted (not even through the admin).

    A new instance is always inserted (``force_insert``): one with the primary
    key of an existing row raises ``IntegrityError`` instead of overwriting it.
    The base manager is the immutable one too, so updates through
    ``_base_manager`` raise. Django's deletion collector updates rows directly
    (``on_delete=SET_NULL``), so the foreign keys of these models use
    ``PROTECT`` instead: that is what keeps a deleted user from changing an
    issued invoice.
    """

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ImmutableInvoiceError(
                f"{type(self).__name__} {self.pk} is issued and cannot be changed."
            )
        if kwargs.get("force_update") or kwargs.get("update_fields") is not None:
            raise ImmutableInvoiceError(
                f"{type(self).__name__} can only be inserted, not updated."
            )
        kwargs["force_insert"] = True
        super().save(*args, **kwargs)

    def save_base(
        self,
        raw=False,
        force_insert=False,
        force_update=False,
        using=None,
        update_fields=None,
    ):
        # Also for a direct call of save_base: only ever an insert.
        if not self._state.adding:
            raise ImmutableInvoiceError(
                f"{type(self).__name__} {self.pk} is issued and cannot be changed."
            )
        if force_update or update_fields is not None:
            raise ImmutableInvoiceError(
                f"{type(self).__name__} can only be inserted, not updated."
            )
        super().save_base(raw=raw, force_insert=True, using=using)

    def delete(self, *args, **kwargs):
        raise ImmutableInvoiceError(
            f"{type(self).__name__} {self.pk} is issued and cannot be deleted."
        )


class InvoiceKind(models.TextChoices):
    INVOICE = "invoice", _("Factuur")
    CREDIT_NOTE = "credit_note", _("Creditnota")


class Invoice(ImmutableModel):
    """
    An invoice or credit note: numbered without gaps per year, and a snapshot of
    everything on it. Amounts include VAT; on a credit note they are negative.

    ``is_test`` is set when the seller details or the VAT rate were missing at
    the time of issue: the PDF gets the watermark "Testfactuur – niet geldig".
    """

    number = models.CharField(_("nummer"), max_length=20, unique=True)
    year = models.PositiveSmallIntegerField(_("jaar"))
    sequence = models.PositiveIntegerField(_("volgnummer"))
    kind = models.CharField(_("soort"), max_length=20, choices=InvoiceKind.choices)
    order = models.ForeignKey(
        Order,
        verbose_name=_("boeking"),
        related_name="invoices",
        on_delete=models.PROTECT,
    )
    credited_invoice = models.ForeignKey(
        "self",
        verbose_name=_("gecrediteerde factuur"),
        related_name="credit_notes",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )
    issue_date = models.DateField(_("factuurdatum"))
    issued_at = models.DateTimeField(_("uitgegeven op"), default=timezone.now)

    seller_name = models.CharField(_("verkoper"), max_length=200, blank=True)
    seller_address = models.TextField(_("adres verkoper"), blank=True)
    seller_kvk = models.CharField(_("KvK-nummer"), max_length=20, blank=True)
    seller_vat_id = models.CharField(_("btw-id"), max_length=30, blank=True)

    customer_name = models.CharField(_("klant"), max_length=201)
    customer_email = models.EmailField(_("e-mail klant"), max_length=100)
    customer_address = models.TextField(_("adres klant"), blank=True)

    hotel_name = models.CharField(_("hotel"), max_length=100)
    hotel_address = models.CharField(_("adres hotel"), max_length=300, blank=True)
    room_type = models.CharField(_("kamer"), max_length=50)
    start_date = models.DateField(_("inchecken"))
    end_date = models.DateField(_("uitchecken"))
    nights = models.PositiveSmallIntegerField(_("nachten"))

    vat_country = models.CharField(_("btw-land"), max_length=2)
    vat_rate = models.DecimalField(
        _("btw-tarief (%)"), max_digits=5, decimal_places=2, null=True, blank=True
    )
    total_excl = models.DecimalField(
        _("totaal excl. btw"), max_digits=10, decimal_places=2
    )
    vat_amount = models.DecimalField(_("btw"), max_digits=10, decimal_places=2)
    total_incl = models.DecimalField(
        _("totaal incl. btw"), max_digits=10, decimal_places=2
    )
    payment_note = models.CharField(_("betaling"), max_length=255, blank=True)
    is_test = models.BooleanField(_("testfactuur"), default=False)

    pdf = models.FileField(
        _("pdf"), storage=get_invoice_storage, upload_to="", max_length=200, blank=True
    )
    pdf_sha256 = models.CharField(_("sha-256 van de pdf"), max_length=64, blank=True)
    # PROTECT: SET_NULL would change an issued invoice. A user who issued an
    # invoice (e.g. cancelled a booking) is deactivated, not deleted.
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("uitgegeven door"),
        related_name="+",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )

    objects = InvoiceManager()

    class Meta:
        verbose_name = _("factuur")
        verbose_name_plural = _("facturen")
        base_manager_name = "objects"
        ordering = ("-issue_date", "-number")
        constraints = [
            models.UniqueConstraint(
                fields=["year", "sequence"], name="billing_invoice_unique_sequence"
            ),
            models.UniqueConstraint(
                fields=["order"],
                condition=Q(kind=InvoiceKind.INVOICE),
                name="billing_invoice_one_per_order",
            ),
            models.UniqueConstraint(
                fields=["credited_invoice"],
                condition=Q(kind=InvoiceKind.CREDIT_NOTE),
                name="billing_invoice_one_credit_note_per_invoice",
            ),
            models.CheckConstraint(
                check=(
                    Q(kind=InvoiceKind.INVOICE, credited_invoice__isnull=True)
                    | Q(kind=InvoiceKind.CREDIT_NOTE, credited_invoice__isnull=False)
                ),
                name="billing_invoice_credit_note_refers_to_invoice",
            ),
        ]
        indexes = [
            models.Index(fields=["issue_date"], name="billing_invoice_date_idx"),
            models.Index(
                fields=["kind", "issue_date"], name="billing_invoice_kind_date_idx"
            ),
        ]

    def __str__(self):
        return self.number

    @property
    def is_credit_note(self):
        return self.kind == InvoiceKind.CREDIT_NOTE

    @property
    def filename(self):
        """The name of the PDF for the guest: ``factuur_2026-00001.pdf``."""
        prefix = "creditnota" if self.is_credit_note else "factuur"
        return f"{prefix}_{self.number}.pdf"


class InvoiceLineKind(models.TextChoices):
    NIGHT = "night", _("Nacht")
    DISCOUNT = "discount", _("Korting")


class InvoiceLine(ImmutableModel):
    """
    One line of an invoice: a night, or a discount (negative).

    ``amount_excl`` is the amount of the line without VAT, for information; the
    VAT of the invoice is computed over the total. ``revenue_excl`` is the
    revenue of the line without VAT with the discounts spread over the nights
    pro rata (discount lines have 0), so the revenue lines add up exactly to
    ``Invoice.total_excl``.
    """

    invoice = models.ForeignKey(
        Invoice,
        verbose_name=_("factuur"),
        related_name="lines",
        on_delete=models.CASCADE,
    )
    position = models.PositiveSmallIntegerField(_("regel"))
    kind = models.CharField(_("soort"), max_length=10, choices=InvoiceLineKind.choices)
    date = models.DateField(_("nacht van"), null=True, blank=True)
    description = models.CharField(_("omschrijving"), max_length=255)
    amount_incl = models.DecimalField(
        _("bedrag incl. btw"), max_digits=10, decimal_places=2
    )
    amount_excl = models.DecimalField(
        _("bedrag excl. btw"), max_digits=10, decimal_places=2
    )
    revenue_excl = models.DecimalField(
        _("omzet excl. btw"), max_digits=10, decimal_places=2
    )

    objects = InvoiceLineManager()

    class Meta:
        verbose_name = _("factuurregel")
        verbose_name_plural = _("factuurregels")
        base_manager_name = "objects"
        ordering = ("invoice", "position")
        constraints = [
            models.UniqueConstraint(
                fields=["invoice", "position"],
                name="billing_invoiceline_unique_position",
            ),
        ]
        indexes = [models.Index(fields=["date"], name="billing_invoiceline_date_idx")]

    def __str__(self):
        return f"{self.invoice_id}/{self.position}: {self.description}"


class VatRate(models.Model):
    """
    The VAT rate for accommodation in a country from ``valid_from`` on. The rate
    of an invoice follows from the country of the hotel and the check-in date.
    Only the superuser manages these, in the admin.
    """

    country = CountryField(_("land"))
    rate = models.DecimalField(
        _("tarief (%)"),
        max_digits=5,
        decimal_places=2,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    valid_from = models.DateField(_("geldig vanaf"))
    note = models.CharField(_("toelichting"), max_length=200, blank=True)

    class Meta:
        verbose_name = _("btw-tarief")
        verbose_name_plural = _("btw-tarieven")
        ordering = ("country", "-valid_from")
        constraints = [
            models.UniqueConstraint(
                fields=["country", "valid_from"], name="billing_vatrate_unique_start"
            ),
        ]

    def __str__(self):
        return f"{self.country} {self.rate}% ({self.valid_from})"


class FakeProviderPayment(models.Model):
    """
    What the fake payment provider (development and CI only) keeps on its side:
    the counterpart of a payment at Mollie.
    """

    id = models.CharField(primary_key=True, max_length=40)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3)
    description = models.CharField(max_length=255, blank=True)
    redirect_url = models.URLField(max_length=500)
    status = models.CharField(max_length=20, default=PaymentState.OPEN)
    method = models.CharField(max_length=30, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = _("nep-betaling")
        verbose_name_plural = _("nep-betalingen")

    def __str__(self):
        return self.id


class FakeProviderRefund(models.Model):
    """A refund at the fake payment provider."""

    id = models.CharField(primary_key=True, max_length=40)
    payment = models.ForeignKey(
        FakeProviderPayment, related_name="refunds", on_delete=models.CASCADE
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3)
    description = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=20, default=RefundState.REFUNDED)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = _("nep-terugbetaling")
        verbose_name_plural = _("nep-terugbetalingen")

    def __str__(self):
        return self.id


# Where demo data may replace everything (see ``check_demo_data_purge``).
DEMO_DATA_ENVIRONMENTS = {"development", "ci", "jenkins"}


class DemoDataPurgeRefused(Exception):
    """Billing data cannot be deleted here, or contains real invoices."""


def check_demo_data_purge():
    """
    Raise ``DemoDataPurgeRefused`` unless demo data may replace the billing
    data: only with ``DEBUG`` or in one of ``DEMO_DATA_ENVIRONMENTS``, and
    never when there is a real invoice (``is_test=False``): those are part of
    the administration and must be kept.
    """
    environment = getattr(settings, "ENVIRONMENT", "")
    if not (settings.DEBUG or environment in DEMO_DATA_ENVIRONMENTS):
        raise DemoDataPurgeRefused(
            f"Not in this environment ({environment or 'unknown'}): only with "
            f"DEBUG or in {', '.join(sorted(DEMO_DATA_ENVIRONMENTS))}."
        )
    real = Invoice.objects.filter(is_test=False).count()
    if real:
        raise DemoDataPurgeRefused(
            f"There are {real} real invoices (without the test watermark); "
            "they are never deleted."
        )


def purge_for_demo_data():
    """
    Delete all invoices, payments, refunds and discount code uses, so
    ``load_demo_data`` can delete the orders (they protect them). Only where
    :func:`check_demo_data_purge` allows it (it raises otherwise). The invoice
    numbers are not reset: a number is never used twice.

    Returns ``{label: count}``.
    """
    check_demo_data_purge()
    counts = {}
    lines = InvoiceLine.objects.all()
    counts["factuurregels"] = models.QuerySet.delete(lines)[0]
    # Credit notes protect the invoice they credit: those go first.
    credit_notes = Invoice.objects.filter(kind=InvoiceKind.CREDIT_NOTE)
    invoices = models.QuerySet.delete(credit_notes)[0]
    invoices += models.QuerySet.delete(Invoice.objects.all())[0]
    counts["facturen"] = invoices
    counts["terugbetalingen"] = Refund.objects.all().delete()[0]
    counts["betalingen"] = Payment.objects.all().delete()[0]
    redemptions = DiscountRedemption.objects.filter(released_at__isnull=True)
    for code_id, used in (
        redemptions.values_list("code_id").annotate(used=models.Count("pk")).order_by()
    ):
        DiscountCode.objects.filter(pk=code_id, times_used__gte=used).update(
            times_used=F("times_used") - used
        )
    counts["kortingscodegebruik"] = DiscountRedemption.objects.all().delete()[0]
    return counts
