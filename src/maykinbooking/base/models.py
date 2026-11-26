import uuid

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Lower, Trim
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from ckeditor.fields import RichTextField
from django_countries.fields import CountryField

from .validators import no_control_characters

upload_storage = FileSystemStorage(
    location=settings.STATIC_ROOT, base_url=settings.STATIC_URL
)


def get_upload_storage():
    """
    Storage of the imported hotel and room photos, which live in the static files.

    The image fields refer to this function instead of to ``upload_storage``
    itself, so migrations contain an import path rather than the absolute
    ``STATIC_ROOT`` of the machine that ran ``makemigrations``. Migrations import
    this function: keep it here.
    """
    return upload_storage


class OrderStatus(models.TextChoices):
    PENDING_PAYMENT = "pending_payment", _("Wacht op betaling")
    CONFIRMED = "confirmed", _("Bevestigd")
    CANCELLED = "cancelled", _("Geannuleerd")
    EXPIRED = "expired", _("Verlopen")


class PaymentStatus(models.TextChoices):
    AT_HOTEL = "at_hotel", _("Betalen in het hotel")
    OPEN = "open", _("Open")
    PAID = "paid", _("Betaald")
    REFUNDED = "refunded", _("Terugbetaald")
    # An online booking that expired without money coming in: there is nothing
    # to pay anymore ("open" would say the guest can still pay).
    UNPAID = "unpaid", _("Niet betaald")


class ActorType(models.TextChoices):
    GUEST = "guest", _("Gast")
    STAFF = "staff", _("Medewerker")
    SYSTEM = "system", _("Systeem")


class StatusField(models.TextChoices):
    """The ``Order`` fields whose changes are logged in ``OrderStatusChange``."""

    STATUS = "status", _("Status")
    PAYMENT_STATUS = "payment_status", _("Betaalstatus")


class EmailKind(models.TextChoices):
    BOOKING_CONFIRMATION = "booking_confirmation", _("Boekingsbevestiging")
    BOOKING_CANCELLATION = "booking_cancellation", _("Annulering")
    PAYMENT_REFUND = "payment_refund", _("Terugbetaling")


class EmailStatus(models.TextChoices):
    PENDING = "pending", _("Wordt verstuurd")
    SENT = "sent", _("Verstuurd")
    FAILED = "failed", _("Mislukt")


def _first_file_url(*files):
    """Return the URL of the first file that is set, or an empty string."""
    for file in files:
        if file:
            try:
                return file.url
            except ValueError:
                continue
    return ""


class City(models.Model):
    city_id = models.CharField(max_length=100)
    name = models.CharField(max_length=100)

    def __str__(self):
        return self.name


class Hotel(models.Model):
    city = models.ForeignKey(City, related_name="test", on_delete=models.CASCADE)
    highlight = models.ManyToManyField("Highlight", related_name="hotel", blank=True)
    hotel_id = models.CharField(max_length=100)
    name = models.CharField(max_length=100)
    address = models.CharField(max_length=200, blank=True, default="")
    image = models.ImageField(
        upload_to="", storage=get_upload_storage, null=True, blank=True
    )
    description = models.TextField(default="", blank=True, null=True)
    price = models.DecimalField(max_digits=6, decimal_places=2, default=0.00)
    is_available = models.BooleanField(default=True)
    terms = RichTextField(blank=True, null=True)
    upload = models.ImageField(upload_to="hotel_img/", null=True, blank=True)
    # Decides the VAT rate of the invoices (billing.VatRate).
    country = CountryField(_("land"), default="NL")

    def __str__(self):
        return self.name

    @property
    def photo_url(self):
        # Photos uploaded via the dashboard take precedence over the imported image.
        return _first_file_url(self.upload, self.image)


SINGLE = "Single"
DOUBLE = "Double"
FAMILY = "Family"

ROOM_CHOICES = (
    (SINGLE, "Single"),
    (DOUBLE, "Double"),
    (FAMILY, "Family"),
)


class Room(models.Model):
    hotel = models.ForeignKey(Hotel, related_name="room", on_delete=models.CASCADE)
    image = models.ImageField(upload_to="", storage=get_upload_storage)
    price = models.DecimalField(max_digits=8, decimal_places=2, default=0.00)
    description = models.TextField()
    is_available = models.BooleanField(default=True)
    room_type = models.CharField(max_length=50, choices=ROOM_CHOICES)
    id = models.AutoField(primary_key=True, editable=False, null=False, default=None)
    upload = models.ImageField(upload_to="room_img/", null=True, blank=True)
    inventory = models.PositiveSmallIntegerField(
        _("voorraad"),
        default=5,
        validators=[MaxValueValidator(999)],
        help_text=_("Aantal kamers van dit type. Bij 0 is de kamer niet te boeken."),
    )

    def __str__(self):
        return self.room_type

    @property
    def photo_url(self):
        return _first_file_url(self.upload, self.image)


class Order(models.Model):
    room = models.ForeignKey(Room, related_name="orders", on_delete=models.PROTECT)
    hotel = models.ForeignKey(Hotel, related_name="orders", on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    start_date = models.DateField()
    end_date = models.DateField()
    first_name = models.CharField(max_length=100, validators=[no_control_characters])
    last_name = models.CharField(max_length=100, validators=[no_control_characters])
    email = models.EmailField(max_length=100)
    address = models.CharField(max_length=100, validators=[no_control_characters])
    zipcode = models.CharField(max_length=6, validators=[no_control_characters])
    city = models.CharField(
        _("woonplaats"),
        max_length=100,
        blank=True,
        validators=[no_control_characters],
        help_text=_("Van de gast. Leeg bij boekingen van voor de facturen."),
    )
    country = CountryField(default="NL")
    token = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    id = models.AutoField(primary_key=True, editable=False, null=False)
    status = models.CharField(
        _("status"),
        max_length=20,
        choices=OrderStatus.choices,
        default=OrderStatus.CONFIRMED,
    )
    payment_status = models.CharField(
        _("betaalstatus"),
        max_length=20,
        choices=PaymentStatus.choices,
        default=PaymentStatus.AT_HOTEL,
    )
    hold_expires_at = models.DateTimeField(
        _("gereserveerd tot"),
        null=True,
        blank=True,
        help_text=_("Alleen bij een boeking die op betaling wacht."),
    )
    # What the guest pays: the sum of the prices of the nights minus the
    # discounts, fixed when the order is made.
    total_price = models.DecimalField(_("totaalprijs"), max_digits=10, decimal_places=2)
    length_discount_percent = models.DecimalField(
        _("korting lang verblijf (%)"), max_digits=5, decimal_places=2, default=0
    )
    length_discount = models.DecimalField(
        _("korting lang verblijf"), max_digits=10, decimal_places=2, default=0
    )
    # Snapshot of the code; billing.DiscountRedemption links the order to it.
    discount_code = models.CharField(_("kortingscode"), max_length=40, blank=True)
    code_discount = models.DecimalField(
        _("korting kortingscode"), max_digits=10, decimal_places=2, default=0
    )
    language = models.CharField(_("taal"), max_length=10, default="nl")

    class Meta:
        permissions = [
            ("cancel_order", _("Kan boekingen annuleren")),
            ("resend_order_email", _("Kan mails over boekingen opnieuw versturen")),
            ("view_customers", _("Kan klanten bekijken")),
            ("view_reports", _("Kan bezetting en omzet bekijken")),
        ]
        indexes = [
            models.Index(fields=["start_date"], name="base_order_start_date_idx"),
            models.Index(fields=["end_date"], name="base_order_end_date_idx"),
            # A customer is an e-mail address, trimmed and lower case.
            models.Index(Lower(Trim("email")), name="base_order_email_key_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                check=Q(end_date__gt=F("start_date")),
                name="base_order_end_after_start",
            ),
            models.CheckConstraint(
                check=Q(status__in=OrderStatus.values), name="base_order_status_valid"
            ),
            models.CheckConstraint(
                check=Q(payment_status__in=PaymentStatus.values),
                name="base_order_payment_status_valid",
            ),
            models.CheckConstraint(
                check=Q(total_price__gte=0), name="base_order_total_price_gte_0"
            ),
            models.CheckConstraint(
                check=Q(length_discount__gte=0, code_discount__gte=0),
                name="base_order_discounts_gte_0",
            ),
        ]

    def __str__(self):
        return str(self.id)

    @property
    def night_count(self):
        return (self.end_date - self.start_date).days

    @property
    def subtotal_price(self):
        """The price of the nights before the discounts."""
        return self.total_price + self.length_discount + self.code_discount


class OrderNight(models.Model):
    """
    One night of an order, with the price that was agreed for it and how it was
    made: ``base_price`` (the room price) with the weekend and season surcharges.
    """

    order = models.ForeignKey(Order, related_name="nights", on_delete=models.CASCADE)
    date = models.DateField(_("nacht van"))
    base_price = models.DecimalField(_("basisprijs"), max_digits=8, decimal_places=2)
    weekend_percent = models.DecimalField(
        _("weekendtoeslag (%)"), max_digits=5, decimal_places=2, default=0
    )
    season_percent = models.DecimalField(
        _("seizoenstoeslag (%)"), max_digits=5, decimal_places=2, default=0
    )
    season_name = models.CharField(_("seizoen"), max_length=100, blank=True)
    price = models.DecimalField(_("prijs"), max_digits=8, decimal_places=2)

    class Meta:
        ordering = ("date",)
        constraints = [
            models.UniqueConstraint(
                fields=["order", "date"], name="base_ordernight_unique_date"
            ),
        ]
        indexes = [models.Index(fields=["date"], name="base_ordernight_date_idx")]

    def __str__(self):
        return f"{self.order_id}: {self.date}"

    @property
    def surcharges(self):
        """
        The labels of the surcharges of this night, e.g. ``["weekend +20%"]``:
        the same labels everywhere (``billing.services.pricing``).
        """
        # Imported here: billing builds on base.
        from maykinbooking.billing.services.pricing import surcharge_labels

        return surcharge_labels(self)


class OrderStatusChange(models.Model):
    """
    Log of the changes of ``Order.status`` and ``Order.payment_status``.

    An empty ``old_value`` means the order was created with ``new_value``.
    """

    order = models.ForeignKey(
        Order, related_name="status_changes", on_delete=models.CASCADE
    )
    field = models.CharField(_("veld"), max_length=20, choices=StatusField.choices)
    old_value = models.CharField(_("oude waarde"), max_length=20, blank=True)
    new_value = models.CharField(_("nieuwe waarde"), max_length=20)
    actor_type = models.CharField(_("door"), max_length=10, choices=ActorType.choices)
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="+",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    # Snapshot of the name, so the log stays readable if the user is deleted.
    actor_label = models.CharField(_("naam"), max_length=150, blank=True)
    reason = models.TextField(_("reden"), blank=True)
    created_at = models.DateTimeField(_("tijdstip"), default=timezone.now)

    class Meta:
        ordering = ("created_at", "id")

    def __str__(self):
        return f"{self.order_id}: {self.field} {self.old_value} -> {self.new_value}"


class EmailLog(models.Model):
    """A mail about an order. The body is not stored, it can be rendered again."""

    order = models.ForeignKey(Order, related_name="emails", on_delete=models.CASCADE)
    kind = models.CharField(_("soort"), max_length=30, choices=EmailKind.choices)
    recipient = models.EmailField(_("ontvanger"))
    subject = models.CharField(_("onderwerp"), max_length=255, blank=True)
    status = models.CharField(
        _("status"),
        max_length=10,
        choices=EmailStatus.choices,
        default=EmailStatus.PENDING,
    )
    error = models.TextField(_("foutmelding"), blank=True)
    triggered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("verstuurd door"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(_("aangemaakt op"), default=timezone.now)
    sent_at = models.DateTimeField(_("verstuurd op"), null=True, blank=True)

    class Meta:
        ordering = ("created_at", "id")
        indexes = [
            models.Index(fields=["order", "created_at"], name="base_emaillog_order_idx")
        ]

    def __str__(self):
        return f"{self.get_kind_display()} {self.recipient}"


class RoomBlock(models.Model):
    """
    Rooms of a type that cannot be booked for a while, e.g. for maintenance.

    The period is half-open like an order: ``end_date`` itself is free again.
    """

    room = models.ForeignKey(Room, related_name="blocks", on_delete=models.CASCADE)
    start_date = models.DateField(_("van"))
    end_date = models.DateField(_("tot"))
    quantity = models.PositiveSmallIntegerField(
        _("aantal kamers"),
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(999)],
    )
    reason = models.CharField(_("reden"), max_length=200, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("aangemaakt door"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(_("aangemaakt op"), default=timezone.now)

    class Meta:
        ordering = ("start_date", "id")
        constraints = [
            models.CheckConstraint(
                check=Q(end_date__gt=F("start_date")),
                name="base_roomblock_end_after_start",
            ),
            models.CheckConstraint(
                check=Q(quantity__gte=1), name="base_roomblock_quantity_min_1"
            ),
        ]
        indexes = [
            models.Index(
                fields=["room", "start_date"], name="base_roomblock_room_start_idx"
            )
        ]

    def __str__(self):
        return f"{self.room_id}: {self.start_date} - {self.end_date} ({self.quantity})"


class Highlight(models.Model):
    icon = models.ImageField(upload_to="highlight_img/", null=True, blank=True)
    name = models.CharField(max_length=100)

    def __str__(self):
        return self.name


class Review(models.Model):
    hotel = models.ForeignKey(Hotel, related_name="review", on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    rating = models.IntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    description = models.TextField()

    def __str__(self):
        return self.name
