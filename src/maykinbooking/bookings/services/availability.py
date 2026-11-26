"""
Availability of rooms per night.

A stay is half-open: ``[start, end)``. The guest sleeps the nights ``start`` up to
and including ``end - 1 day``; on ``end`` the room is free for the next guest.

A night of a room type is occupied by:

* every ``OrderNight`` of an order that is confirmed, or that waits for payment
  and whose hold has not expired yet (see :func:`occupying_orders_q`);
* every ``RoomBlock`` that covers the night, with its ``quantity``.

``Room.inventory`` minus the busiest night of the stay is the number of rooms of
that type that can still be booked. Only portable SQL (COUNT and GROUP BY), so the
same code runs on SQLite and PostgreSQL.
"""
from collections import defaultdict
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db.models import Count, Max, Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from maykinbooking.base.models import OrderNight, OrderStatus, Room, RoomBlock

MAX_NIGHTS = 30
# How far ahead a guest can book: the day of arrival is at most this many days
# after today.
MAX_DAYS_AHEAD = 365

ONE_DAY = timedelta(days=1)


def validate_stay_period(start, end, *, today=None):
    """
    Check the period of a stay. Raises ``ValidationError`` with one of the codes
    ``past``, ``end_before_start``, ``too_long`` or ``too_far`` (arrival more
    than ``MAX_DAYS_AHEAD`` days after today).
    """
    today = today or timezone.localdate()
    if start < today:
        raise ValidationError(_("Je kunt niet inchecken in het verleden."), code="past")
    if end <= start:
        raise ValidationError(
            _("Uitchecken moet na de incheckdatum zijn."), code="end_before_start"
        )
    if (end - start).days > MAX_NIGHTS:
        raise ValidationError(
            _("Je kunt maximaal %(max_nights)s nachten boeken."),
            code="too_long",
            params={"max_nights": MAX_NIGHTS},
        )
    if (start - today).days > MAX_DAYS_AHEAD:
        raise ValidationError(
            _("Je kunt maximaal %(max_days)s dagen vooruit boeken."),
            code="too_far",
            params={"max_days": MAX_DAYS_AHEAD},
        )


def occupying_orders_q(*, now=None, prefix=""):
    """
    ``Q`` for the orders that occupy their nights: confirmed, or waiting for
    payment with a hold that has not expired yet.

    Use ``prefix`` to filter related models, e.g. ``prefix="order__"`` on
    ``OrderNight``.
    """
    now = now or timezone.now()
    return Q(**{f"{prefix}status": OrderStatus.CONFIRMED}) | Q(
        **{
            f"{prefix}status": OrderStatus.PENDING_PAYMENT,
            f"{prefix}hold_expires_at__gt": now,
        }
    )


def occupancy(room_ids, start, end, *, now=None):
    """
    Occupied rooms per room type per night in ``[start, end)``.

    Returns ``{room_id: {date: count}}`` with a key for every room id; nights
    without occupation are left out. Two queries, whatever the number of rooms.
    """
    room_ids = list(room_ids)
    result = {room_id: defaultdict(int) for room_id in room_ids}
    if not room_ids or end <= start:
        return {room_id: {} for room_id in room_ids}

    nights = (
        OrderNight.objects.filter(
            occupying_orders_q(now=now, prefix="order__"),
            order__room_id__in=room_ids,
            date__gte=start,
            date__lt=end,
        )
        .values("order__room_id", "date")
        .annotate(count=Count("id"))
        .order_by()
    )
    for row in nights:
        result[row["order__room_id"]][row["date"]] += row["count"]

    blocks = RoomBlock.objects.filter(
        room_id__in=room_ids, start_date__lt=end, end_date__gt=start
    ).values_list("room_id", "start_date", "end_date", "quantity")
    for room_id, block_start, block_end, quantity in blocks:
        day = max(block_start, start)
        while day < min(block_end, end):
            result[room_id][day] += quantity
            day += ONE_DAY

    return {room_id: dict(per_night) for room_id, per_night in result.items()}


def free_counts(rooms, start, end, *, now=None):
    """
    Number of rooms per room type that can still be booked for the whole stay:
    ``inventory`` minus the busiest night, never below 0.

    ``rooms`` are ``Room`` instances. Does not look at ``is_available``.
    Returns ``{room_id: count}``.
    """
    rooms = list(rooms)
    occupied = occupancy([room.pk for room in rooms], start, end, now=now)
    return {
        room.pk: max(0, room.inventory - max(occupied[room.pk].values(), default=0))
        for room in rooms
    }


def available_rooms(rooms_qs, start, end):
    """
    The rooms of ``rooms_qs`` that can be booked for ``[start, end)``: the room
    and its hotel are available and at least one room of the type is free.

    Returns ``rooms_qs`` filtered on primary key, so its ordering and
    ``select_related`` stay intact.
    """
    candidates = rooms_qs.filter(
        is_available=True, hotel__is_available=True, inventory__gt=0
    )
    free = free_counts(candidates, start, end)
    return rooms_qs.filter(pk__in=[pk for pk, count in free.items() if count >= 1])


def available_hotel_ids(hotels_qs, start, end):
    """
    Ids of the hotels in ``hotels_qs`` with at least one bookable room for
    ``[start, end)``. Three queries, whatever the number of hotels.
    """
    rooms = list(
        Room.objects.filter(
            hotel__in=hotels_qs.order_by().values("pk"),
            is_available=True,
            hotel__is_available=True,
            inventory__gt=0,
        ).only("pk", "hotel_id", "inventory")
    )
    free = free_counts(rooms, start, end)
    return {room.hotel_id for room in rooms if free[room.pk] >= 1}


def _overbooked(occupied, inventory):
    return sorted((day, count) for day, count in occupied.items() if count > inventory)


def overbooked_nights(room, *, inventory=None, from_date=None):
    """
    Nights from ``from_date`` (default: today) on which more rooms are occupied
    than ``inventory`` (default: ``room.inventory``). For warnings when the
    inventory is lowered or a block is added; nothing is cancelled.

    Returns ``[(date, occupied), ...]`` sorted by date.
    """
    inventory = room.inventory if inventory is None else inventory
    from_date = from_date or timezone.localdate()

    last_night = OrderNight.objects.filter(
        order__room_id=room.pk, date__gte=from_date
    ).aggregate(last=Max("date"))["last"]
    last_block_end = RoomBlock.objects.filter(
        room_id=room.pk, end_date__gt=from_date
    ).aggregate(last=Max("end_date"))["last"]
    ends = [day for day in (last_night and last_night + ONE_DAY, last_block_end) if day]
    if not ends:
        return []

    occupied = occupancy([room.pk], from_date, max(ends))[room.pk]
    return _overbooked(occupied, inventory)
