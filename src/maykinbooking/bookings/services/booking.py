"""
Booking a room, changing the status of an order and cancelling it.

Every change of ``Order.status`` or ``Order.payment_status`` goes through this
module, so it is logged in ``OrderStatusChange``. Functions that change an order
lock it (``select_for_update``) in a transaction; :func:`transition` and
:func:`set_payment_status` expect the caller to have done that.

Mails are sent with ``transaction.on_commit``: never for a booking that is rolled
back, and a failing mail never undoes a booking.
"""
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from functools import partial
from typing import Any, List, Optional, Tuple

from django.core.exceptions import ValidationError
from django.db import transaction
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

from .availability import free_counts, overbooked_nights, validate_stay_period

GUEST_FIELDS = ("first_name", "last_name", "email", "address", "zipcode", "country")

ALLOWED_TRANSITIONS = {
    OrderStatus.PENDING_PAYMENT: {OrderStatus.CONFIRMED, OrderStatus.EXPIRED},
    OrderStatus.CONFIRMED: {OrderStatus.CANCELLED},
}


class BookingError(Exception):
    """Base class of the errors of the booking service."""


class RoomUnavailable(BookingError):
    """No room of this type can be booked for the period (anymore)."""


class InvalidTransition(BookingError):
    """The order cannot go from its current status to the requested one."""


class CancellationNotAllowed(BookingError):
    """
    The order can no longer be cancelled: the stay is over, or (for the guest)
    the free cancellation deadline has passed or the order is not confirmed.
    """


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
    nights: List[Tuple[Any, Decimal]]  # [(date, price), ...]
    total: Decimal


def quote(room, start, end):
    """
    The price of every night of ``[start, end)`` and the total.

    Phase 1: every night costs ``room.price``. Price rules (season, discounts)
    belong here, so the checkout summary and the booking always agree.
    """
    nights = [
        (start + timedelta(days=offset), room.price)
        for offset in range((end - start).days)
    ]
    return Quote(
        nights=nights, total=sum((price for day, price in nights), Decimal("0.00"))
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


def _send_email_on_commit(order, kind, *, actor):
    # Imported here because the emails module imports this one.
    from ..emails import send_booking_email

    transaction.on_commit(
        partial(send_booking_email, order.pk, kind, triggered_by=actor.user)
    )


def create_booking(*, room_id, start, end, guest, language, actor, token=None):
    """
    Book one room of type ``room_id`` for ``[start, end)``.

    ``guest`` holds the guest fields of the order (``GUEST_FIELDS``; other keys
    are ignored). The room is locked first, then everything is checked again, so
    two guests can never get the last room at the same time.

    ``token`` is an optional idempotency key (a UUID) that becomes
    ``Order.token``. If an order with that token already exists, that order is
    returned: nothing is booked or mailed again. This catches a booking that is
    sent twice (double click, a replayed request). Two requests for different
    rooms with the same token can still meet at the unique constraint:
    ``IntegrityError``.

    Raises ``ValidationError`` (invalid period) or ``RoomUnavailable``. Returns
    the confirmed order (payment at the hotel); the confirmation mail is sent
    after the commit.
    """
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
                return existing

        validate_stay_period(start, end)
        if not (room.is_available and room.hotel.is_available):
            raise RoomUnavailable(_("Deze kamer is niet te boeken."))
        if free_counts([room], start, end)[room.pk] < 1:
            raise RoomUnavailable(_("Deze kamer is niet meer vrij in deze periode."))

        offer = quote(room, start, end)
        order = Order.objects.create(
            room=room,
            hotel=room.hotel,
            start_date=start,
            end_date=end,
            status=OrderStatus.CONFIRMED,
            payment_status=PaymentStatus.AT_HOTEL,
            total_price=offer.total,
            language=language,
            **({"token": token} if token is not None else {}),
            **{field: guest[field] for field in GUEST_FIELDS if field in guest},
        )
        OrderNight.objects.bulk_create(
            OrderNight(order=order, date=day, price=price)
            for day, price in offer.nights
        )
        _log_change(order, StatusField.STATUS, "", order.status, actor=actor)
        _log_change(
            order, StatusField.PAYMENT_STATUS, "", order.payment_status, actor=actor
        )
        _send_email_on_commit(order, EmailKind.BOOKING_CONFIRMATION, actor=actor)
    return order


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

    Returns ``(order, changed)``; ``changed`` is ``False`` if the order was
    already cancelled (no log, no mail). Raises ``Order.DoesNotExist``,
    ``CancellationNotAllowed`` (the stay is over, for guests and staff alike; or
    ``enforce_deadline`` and the guest can no longer cancel), ``ValueError``
    (staff without a reason) or ``InvalidTransition`` (e.g. an expired order).

    Guests: ``Actor.guest()`` with ``enforce_deadline=True``. Staff:
    ``Actor.staff(user)`` with a reason and ``enforce_deadline=False``.
    """
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
        _send_email_on_commit(order, EmailKind.BOOKING_CANCELLATION, actor=actor)
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
    have no hold). Returns the number of expired orders.
    """
    now = now or timezone.now()
    stale = Order.objects.filter(
        Q(hold_expires_at__isnull=True) | Q(hold_expires_at__lte=now),
        status=OrderStatus.PENDING_PAYMENT,
    )
    expired = 0
    for order_id in list(stale.values_list("pk", flat=True)):
        with transaction.atomic():
            # Checked again after the lock: the order may be paid meanwhile.
            order = stale.select_for_update().filter(pk=order_id).first()
            if order is None:
                continue
            if transition(
                order,
                OrderStatus.EXPIRED,
                actor=Actor.system(),
                reason=gettext("Betaaltermijn verlopen"),
            ):
                expired += 1
    return expired
