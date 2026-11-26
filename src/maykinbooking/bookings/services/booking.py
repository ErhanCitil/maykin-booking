"""
Booking a room, changing the status of an order and cancelling it.

Every change of ``Order.status`` or ``Order.payment_status`` goes through this
module, so it is logged in ``OrderStatusChange``. Functions that change an order
lock it (``select_for_update``) in a transaction; :func:`transition` and
:func:`set_payment_status` expect the caller to have done that.

Mails are sent with ``transaction.on_commit``: never for a booking that is rolled
back, and a failing mail never undoes a booking.

Prices come from ``billing.services.pricing`` (price rules, discount codes) and
invoices from ``billing.services.invoicing``. The payments service imports this
module, so it is only imported inside the functions that need it.

Locks are always taken in this order: Room → Order → Payment/Refund →
DiscountCode → InvoiceSequence.
"""
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any, List, Optional

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext, gettext_lazy as _

from maykinbooking.base.models import (
    ActorType,
    EmailKind,
    Order,
    OrderNight,
    OrderStatus,
    OrderStatusChange,
    PaymentStatus,
    Room,
    RoomBlock,
    StatusField,
)
from maykinbooking.billing.models import DiscountCode, InvoiceKind, RefundReason
from maykinbooking.billing.providers import online_payment_available
from maykinbooking.billing.services.invoicing import issue_credit_note, issue_invoice
from maykinbooking.billing.services.pricing import (
    ZERO,
    NightPrice,
    PricingRules,
    check_discount_code,
    discount_amount,
    length_discount,
    night_price,
    redeem_discount_code,
    release_discount_code,
)
from maykinbooking.utils.transactions import on_commit_safely

from ..exceptions import BookingError
from .availability import free_counts, overbooked_nights, validate_stay_period

GUEST_FIELDS = (
    "first_name",
    "last_name",
    "email",
    "address",
    "zipcode",
    "city",
    "country",
)

ALLOWED_TRANSITIONS = {
    OrderStatus.PENDING_PAYMENT: {OrderStatus.CONFIRMED, OrderStatus.EXPIRED},
    OrderStatus.CONFIRMED: {OrderStatus.CANCELLED},
}


class PaymentMethod(models.TextChoices):
    """How the guest pays: in the hotel (the default) or online beforehand."""

    AT_HOTEL = "at_hotel", _("Betalen in het hotel")
    ONLINE = "online", _("Online betalen met iDEAL")


class RoomUnavailable(BookingError):
    """No room of this type can be booked for the period (anymore)."""


class InvalidTransition(BookingError):
    """The order cannot go from its current status to the requested one."""


class CancellationNotAllowed(BookingError):
    """
    The order can no longer be cancelled: the stay is over, or (for the guest)
    the free cancellation deadline has passed or the order is not confirmed.
    """


class OnlinePaymentUnavailable(BookingError):
    """
    Online payment is not possible for this booking: no payment provider is
    set up, or there is nothing to pay (the total is 0). Nothing was booked.
    """


class PriceChanged(BookingError):
    """
    The price of the stay is no longer what the guest saw (``expected_total``):
    the price rules or the discount code changed meanwhile. ``quote`` is the
    new price; nothing was booked.
    """

    def __init__(self, quote):
        super().__init__(
            _("De prijs van dit verblijf is gewijzigd. Bekijk de nieuwe prijs.")
        )
        self.quote = quote


@dataclass(frozen=True)
class Actor:
    """Who makes a change: the guest, a staff member or the system."""

    type: str
    user: Optional[Any] = None

    @classmethod
    def guest(cls):
        return cls(ActorType.GUEST)

    @classmethod
    def system(cls):
        return cls(ActorType.SYSTEM)

    @classmethod
    def staff(cls, user):
        if user is None:
            raise ValueError("A staff actor needs a user.")
        return cls(ActorType.STAFF, user)

    @property
    def label(self):
        """Name for the log, stored as a snapshot in ``actor_label``."""
        if self.user is not None:
            name = self.user.get_full_name().strip() or self.user.get_username()
            return name[:150]
        return str(ActorType(self.type).label)


@dataclass(frozen=True)
class Quote:
    """
    The price of a stay. ``total`` = ``subtotal`` - ``length_discount`` -
    ``code_discount``, never below 0. ``discount`` is the ``DiscountCode`` or
    ``None``; ``length_discount_percent`` is 0 when there is no discount for a
    long stay.
    """

    nights: List[NightPrice]
    subtotal: Decimal
    length_discount_percent: Decimal
    length_discount: Decimal
    discount: Optional[DiscountCode]
    code_discount: Decimal
    total: Decimal

    @property
    def discount_code(self):
        """The code as on an order (``""`` without a code)."""
        return self.discount.code if self.discount else ""


@dataclass(frozen=True)
class BookingResult:
    """
    The order of :func:`book_room`, and whether this call made it (``False``:
    an earlier request with the same token did; nothing was booked again).
    """

    order: Order
    created: bool


def quote(room, start, end, *, discount_code="", rules=None, today=None):
    """
    The price of every night of ``[start, end)``, the discounts and the total.
    The checkout summary and the booking both use this, so they always agree.

    In this order: the nights (weekend and season surcharges), their subtotal,
    the discount for a long stay over the subtotal, and the discount code over
    what is left (a fixed amount takes at most all of it).

    ``rules``: ``PricingRules`` (default: the current rules). Raises
    ``InvalidDiscountCode`` for a ``discount_code`` that cannot be used.
    """
    if rules is None:
        rules = PricingRules.load(start, end)
    nights = [
        night_price(room.price, start + timedelta(days=offset), rules)
        for offset in range((end - start).days)
    ]
    subtotal = sum((night.price for night in nights), ZERO)
    length_percent, length_amount = length_discount(subtotal, len(nights), rules)
    discount = None
    code_amount = ZERO
    if discount_code:
        discount = check_discount_code(discount_code, today=today)
        code_amount = discount_amount(discount, subtotal - length_amount)
    total = max(subtotal - length_amount - code_amount, ZERO)
    return Quote(
        nights=nights,
        subtotal=subtotal,
        length_discount_percent=length_percent,
        length_discount=length_amount,
        discount=discount,
        code_discount=code_amount,
        total=total,
    )


def _log_change(order, field, old_value, new_value, *, actor, reason=""):
    OrderStatusChange.objects.create(
        order=order,
        field=field,
        old_value=old_value,
        new_value=new_value,
        actor_type=actor.type,
        actor_user=actor.user,
        actor_label=actor.label,
        reason=reason,
    )


def send_email_on_commit(order, kind, *, actor, **options):
    """
    Send the mail ``kind`` about ``order`` after the commit (never raises).
    ``options`` go to ``send_booking_email`` (e.g. ``refund_id``).
    """
    # Imported here because the emails module imports this one.
    from ..emails import send_booking_email

    on_commit_safely(
        send_booking_email, order.pk, kind, triggered_by=actor.user, **options
    )


def create_booking(**booking):
    """
    :func:`book_room`, returning only the order (made now or before, with the
    same token). For callers that do not need to know which.
    """
    return book_room(**booking).order


def book_room(
    *,
    room_id,
    start,
    end,
    guest,
    language,
    actor,
    token=None,
    payment_method=PaymentMethod.AT_HOTEL,
    discount_code="",
    expected_total=None,
):
    """
    Book one room of type ``room_id`` for ``[start, end)``. Returns a
    :class:`BookingResult`.

    ``guest`` holds the guest fields of the order (``GUEST_FIELDS``; other keys
    are ignored). The room is locked first, then everything is checked again, so
    two guests can never get the last room at the same time.

    ``token`` is an optional idempotency key (a UUID) that becomes
    ``Order.token``. If an order with that token already exists, that order is
    returned with ``created=False``: nothing is booked or mailed again. This
    catches a booking that is sent twice (double click, a replayed request): a
    second request for the same room waits for the lock of the first and then
    finds its order; one for another room meets the first at the unique token,
    and finds the order after that.

    The price is :func:`quote` with the current price rules and
    ``discount_code``; the nights and discounts are stored on the order as a
    snapshot. ``expected_total``: the total the guest saw; if the price is
    different now, ``PriceChanged`` is raised and nothing is booked.

    ``payment_method``:

    * ``PaymentMethod.AT_HOTEL``: the order is confirmed, gets its invoice, and
      the confirmation mail is sent after the commit.
    * ``PaymentMethod.ONLINE``: the order waits for payment and holds the room
      for ``PAYMENT_HOLD_MINUTES``; no mail, no invoice. Start the payment with
      ``billing.services.payments.start_online_payment``. Only when online
      payment is available and the total is above 0, otherwise
      ``OnlinePaymentUnavailable``.

    Raises ``ValidationError`` (invalid period), ``RoomUnavailable``,
    ``InvalidDiscountCode`` (also when the last use of the code was taken
    meanwhile), ``PriceChanged`` or ``OnlinePaymentUnavailable``; nothing is
    booked then. ``ValueError`` for an unknown ``payment_method``.
    """
    if payment_method not in PaymentMethod.values:
        raise ValueError(f"Unknown payment method {payment_method!r}.")
    try:
        return _book_room(
            room_id=room_id,
            start=start,
            end=end,
            guest=guest,
            language=language,
            actor=actor,
            token=token,
            online=payment_method == PaymentMethod.ONLINE,
            discount_code=discount_code,
            expected_total=expected_total,
        )
    except IntegrityError:
        # Another request with this token (for another room) won the race.
        existing = (
            Order.objects.filter(token=token).first() if token is not None else None
        )
        if existing is None:
            raise
        return BookingResult(existing, created=False)


def _book_room(
    *,
    room_id,
    start,
    end,
    guest,
    language,
    actor,
    token,
    online,
    discount_code,
    expected_total,
):
    with transaction.atomic():
        try:
            room = (
                Room.objects.select_for_update(of=("self",))
                .select_related("hotel")
                .get(pk=room_id)
            )
        except Room.DoesNotExist:
            raise RoomUnavailable(_("Deze kamer bestaat niet (meer)."))

        # After the lock: a second request for the same booking waits for the
        # first one and then finds its order here.
        if token is not None:
            existing = Order.objects.filter(token=token).first()
            if existing is not None:
                return BookingResult(existing, created=False)

        validate_stay_period(start, end)
        if not (room.is_available and room.hotel.is_available):
            raise RoomUnavailable(_("Deze kamer is niet te boeken."))
        if free_counts([room], start, end)[room.pk] < 1:
            raise RoomUnavailable(_("Deze kamer is niet meer vrij in deze periode."))

        today = timezone.localdate()
        offer = quote(room, start, end, discount_code=discount_code, today=today)
        if expected_total is not None and offer.total != Decimal(expected_total):
            raise PriceChanged(offer)
        if online and not online_payment_available():
            raise OnlinePaymentUnavailable(_("Online betalen is nu niet mogelijk."))
        if online and offer.total <= 0:
            raise OnlinePaymentUnavailable(
                _("Er valt niets online te betalen: het totaal is € 0,00.")
            )

        hold = timedelta(minutes=settings.PAYMENT_HOLD_MINUTES)
        order = Order.objects.create(
            room=room,
            hotel=room.hotel,
            start_date=start,
            end_date=end,
            status=OrderStatus.PENDING_PAYMENT if online else OrderStatus.CONFIRMED,
            payment_status=PaymentStatus.OPEN if online else PaymentStatus.AT_HOTEL,
            hold_expires_at=timezone.now() + hold if online else None,
            total_price=offer.total,
            length_discount_percent=offer.length_discount_percent,
            length_discount=offer.length_discount,
            discount_code=offer.discount_code,
            code_discount=offer.code_discount,
            language=language,
            **({"token": token} if token is not None else {}),
            **{field: guest[field] for field in GUEST_FIELDS if field in guest},
        )
        OrderNight.objects.bulk_create(
            OrderNight(
                order=order,
                date=night.date,
                base_price=night.base_price,
                weekend_percent=night.weekend_percent,
                season_percent=night.season_percent,
                season_name=night.season_name,
                price=night.price,
            )
            for night in offer.nights
        )
        if offer.discount is not None:
            redeem_discount_code(
                offer.discount.code, order, offer.code_discount, today=today
            )
        _log_change(order, StatusField.STATUS, "", order.status, actor=actor)
        _log_change(
            order, StatusField.PAYMENT_STATUS, "", order.payment_status, actor=actor
        )
        if not online:
            issue_invoice(order, actor=actor.user)
            send_email_on_commit(order, EmailKind.BOOKING_CONFIRMATION, actor=actor)
    return BookingResult(order, created=True)


def transition(order, new_status, *, actor, reason=""):
    """
    Change ``order.status`` and log it. The caller must have locked the order.

    Returns ``False`` if the order already has ``new_status`` (nothing happens),
    ``True`` after a change. Raises ``InvalidTransition`` for a change that is
    not in ``ALLOWED_TRANSITIONS``.
    """
    old_status = order.status
    if new_status == old_status:
        return False
    if new_status not in ALLOWED_TRANSITIONS.get(old_status, ()):
        raise InvalidTransition(
            f"Order {order.pk} cannot go from {old_status!r} to {new_status!r}."
        )
    order.status = new_status
    order.save(update_fields=["status"])
    _log_change(
        order, StatusField.STATUS, old_status, new_status, actor=actor, reason=reason
    )
    return True


def expire_order(order, *, actor, reason):
    """
    Let an order that waits for payment expire: its room and its discount code
    are free again, and a payment status that is still open becomes "unpaid"
    (nothing came in, and nothing can be paid anymore). The caller must have
    locked the order. Returns ``False`` if it had expired already; raises
    ``InvalidTransition`` for other statuses.
    """
    changed = transition(order, OrderStatus.EXPIRED, actor=actor, reason=reason)
    if changed:
        release_discount_code(order)
        if order.payment_status == PaymentStatus.OPEN:
            set_payment_status(order, PaymentStatus.UNPAID, actor=actor, reason=reason)
    return changed


def set_payment_status(order, new, *, actor, reason=""):
    """
    Change ``order.payment_status`` and log it. The caller must have locked the
    order. Returns ``False`` if nothing changed. Raises ``ValueError`` for an
    unknown payment status.
    """
    if new not in PaymentStatus.values:
        raise ValueError(f"Unknown payment status {new!r}.")
    old = order.payment_status
    if new == old:
        return False
    order.payment_status = new
    order.save(update_fields=["payment_status"])
    _log_change(order, StatusField.PAYMENT_STATUS, old, new, actor=actor, reason=reason)
    return True


def guest_cancellation_deadline(order):
    """
    Until when the guest can cancel for free: 00:00 Europe/Amsterdam (the
    default time zone) on the day before arrival. Aware datetime.
    """
    day_before = order.start_date - timedelta(days=1)
    return timezone.make_aware(
        datetime.combine(day_before, time.min), timezone.get_default_timezone()
    )


def can_guest_cancel(order, *, now=None):
    now = now or timezone.now()
    return order.status == OrderStatus.CONFIRMED and now < guest_cancellation_deadline(
        order
    )


def stay_is_over(order, *, today=None):
    """
    True once the stay has ended: from the day after the departure day on.

    On the departure day itself the stay is not over yet, so staff can still
    cancel it, e.g. for a guest who did not show up for a single night. There is
    no separate status for this; it follows from the dates. A stay that is over
    can no longer be cancelled, by the guest or by staff. (Guests cannot cancel
    that late anyway: their free cancellation ends before arrival.)
    """
    today = today or timezone.localdate()
    return order.end_date < today


def can_staff_cancel(order, *, today=None):
    """Staff can cancel a confirmed booking at any time until the stay is over."""
    return order.status == OrderStatus.CONFIRMED and not stay_is_over(
        order, today=today
    )


# The mail that staff can send again depends on the status: the confirmation
# says the booking stands, so a cancelled booking gets the cancellation instead.
RESEND_KINDS = {
    OrderStatus.CONFIRMED: EmailKind.BOOKING_CONFIRMATION,
    OrderStatus.CANCELLED: EmailKind.BOOKING_CANCELLATION,
}


def resend_kind(order):
    """The kind of mail staff can send again for this order, or ``None``."""
    return RESEND_KINDS.get(order.status)


def cancel_booking(order_id, *, actor, reason="", enforce_deadline=True):
    """
    Cancel an order and send the cancellation mail after the commit.

    An order with an invoice gets a credit note in the same transaction. A paid
    order gets a refund of the full amount, which is sent to the payment
    provider after the commit (after the cancellation mail); if that fails the
    order stays cancelled and paid, and the refund is tried again later
    (``billing.services.payments``).

    Returns ``(order, changed)``; ``changed`` is ``False`` if the order was
    already cancelled (no log, no mail). Raises ``Order.DoesNotExist``,
    ``CancellationNotAllowed`` (the stay is over, for guests and staff alike; or
    ``enforce_deadline`` and the guest can no longer cancel), ``ValueError``
    (staff without a reason) or ``InvalidTransition`` (e.g. an expired order).

    Guests: ``Actor.guest()`` with ``enforce_deadline=True``. Staff:
    ``Actor.staff(user)`` with a reason and ``enforce_deadline=False``.
    """
    # Imported here: the payments service imports this module.
    from maykinbooking.billing.services.payments import execute_refund, request_refund

    reason = reason.strip()
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order_id)
        if order.status == OrderStatus.CANCELLED:
            return order, False
        if stay_is_over(order):
            raise CancellationNotAllowed(
                _("Dit verblijf is voorbij en kan niet meer geannuleerd worden.")
            )
        if enforce_deadline and not can_guest_cancel(order):
            raise CancellationNotAllowed(
                _("Deze boeking kan niet meer kosteloos geannuleerd worden.")
            )
        if actor.type == ActorType.STAFF and not reason:
            raise ValueError("Staff must give a reason to cancel a booking.")
        transition(order, OrderStatus.CANCELLED, actor=actor, reason=reason)
        # Lock order: the payment (refund) before the invoice numbering.
        refund = None
        if order.payment_status == PaymentStatus.PAID:
            refund = request_refund(
                order, reason=RefundReason.CANCELLATION, actor=actor
            )
        invoice = order.invoices.filter(kind=InvoiceKind.INVOICE).first()
        if invoice is not None:
            issue_credit_note(invoice, actor=actor.user)
        send_email_on_commit(order, EmailKind.BOOKING_CANCELLATION, actor=actor)
        if refund is not None:
            on_commit_safely(execute_refund, refund.pk)
    return order, True


def create_room_block(room_id, *, start, end, quantity, reason, user):
    """
    Block ``quantity`` rooms of type ``room_id`` for ``[start, end)``.

    The block is always created, also when it overbooks: existing orders are
    never cancelled. Returns ``(block, overbooked)``, where ``overbooked`` is
    ``[(date, occupied), ...]`` for the nights in the period on which more rooms
    are occupied than the inventory. Raises ``ValidationError`` with code
    ``end_before_start`` or ``quantity``, or ``Room.DoesNotExist``.
    """
    if end <= start:
        raise ValidationError(
            _("De einddatum moet na de begindatum liggen."), code="end_before_start"
        )
    if quantity < 1:
        raise ValidationError(_("Blokkeer minstens één kamer."), code="quantity")
    with transaction.atomic():
        # The same lock as create_booking: a booking waits for the block, and
        # the other way around.
        room = Room.objects.select_for_update().get(pk=room_id)
        block = RoomBlock.objects.create(
            room=room,
            start_date=start,
            end_date=end,
            quantity=quantity,
            reason=reason,
            created_by=user,
        )
        overbooked = [
            (day, occupied)
            for day, occupied in overbooked_nights(room, from_date=start)
            if day < end
        ]
    return block, overbooked


def expire_stale_pending(*, now=None):
    """
    Expire the orders that wait for payment and whose hold has passed (or that
    have no hold); their rooms and discount codes are free again. Returns the
    number of expired orders.

    Their open payments are synced with the provider first: an order that was
    paid meanwhile is confirmed instead. An order whose payment is still open
    at the provider is left alone for ``PAYMENT_GRACE`` after its hold, so a
    guest who is still paying can finish. When the provider cannot be reached,
    the order is left for the next round, up to 24 hours after its hold.
    """
    # Imported here: the payments service imports this module.
    from maykinbooking.billing.services.payments import sync_pending_order_payments

    now = now or timezone.now()
    stale = Order.objects.filter(
        Q(hold_expires_at__isnull=True) | Q(hold_expires_at__lte=now),
        status=OrderStatus.PENDING_PAYMENT,
    )
    skip = sync_pending_order_payments(stale.values_list("pk", flat=True), now=now)
    expired = 0
    for order_id in list(stale.exclude(pk__in=skip).values_list("pk", flat=True)):
        with transaction.atomic():
            # Checked again after the lock: the order may be paid meanwhile.
            order = stale.select_for_update().filter(pk=order_id).first()
            if order is None:
                continue
            if expire_order(
                order,
                actor=Actor.system(),
                reason=gettext("Betaaltermijn verlopen"),
            ):
                expired += 1
    return expired
