"""
Occupancy and revenue per hotel (dashboard "Rapportage").

A period is half-open like a stay: ``[start, end)`` are the nights ``start`` up to
and including ``end - 1 day``. Per hotel:

* capacity: the rooms of all its room types (the current ``inventory``) times
  the nights of the period;
* blocked: the room nights taken out by ``RoomBlock``; per room type and night
  at most its inventory, also when blocks overlap;
* booked: the nights of confirmed bookings;
* occupancy: booked / (capacity - blocked), or ``None`` when nothing could be
  booked;
* revenue: the revenue without VAT of the invoiced nights in the period
  (``InvoiceLine.revenue_excl``, with the discounts spread over the nights).
  Credit notes have negative lines, so the revenue is net. Orders from before
  invoicing have no invoice and no revenue.
* untaxed: invoices (and credit notes) without a VAT rate (``vat_rate`` is
  null: there was no rate for the country of the hotel) are left out of the
  revenue, because their amount without VAT is not known. They are counted
  instead, with their amount including VAT, so the report says what is
  missing. Not ``is_test``: an invoice is a test invoice when the seller
  details are missing, while its VAT is right.

:func:`hotel_report` takes five queries, whatever the number of hotels.
"""
import calendar
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from django.db.models import Count, Q, Sum
from django.utils import timezone

from maykinbooking.base.models import Hotel, OrderNight, OrderStatus, Room, RoomBlock
from maykinbooking.billing.models import InvoiceLine, InvoiceLineKind

ZERO = Decimal("0.00")
ONE_DAY = timedelta(days=1)
# A report covers at most this many nights (a leap year).
MAX_REPORT_NIGHTS = 366


@dataclass
class HotelRow:
    hotel_id: Optional[int]
    name: str
    city: str = ""
    capacity: int = 0
    blocked: int = 0
    booked: int = 0
    revenue: Decimal = ZERO
    # Invoices without a VAT rate, not in the revenue, and their amount incl. VAT.
    untaxed_invoices: int = 0
    untaxed_incl: Decimal = ZERO

    @property
    def available(self):
        """The room nights that could be booked: capacity minus blocked."""
        return self.capacity - self.blocked

    @property
    def occupancy(self) -> Optional[Decimal]:
        """Booked as a percentage of the available room nights, one decimal."""
        if self.available <= 0:
            return None
        return (Decimal(self.booked) * 100 / self.available).quantize(
            Decimal("0.1"), rounding=ROUND_HALF_UP
        )


def month_period(day):
    """``(first day, first day of the next month)`` of the month of ``day``."""
    start = day.replace(day=1)
    days = calendar.monthrange(day.year, day.month)[1]
    return start, start + timedelta(days=days)


def current_month(today=None):
    return month_period(today or timezone.localdate())


# The VAT rate is known: the line counts in the revenue.
TAXED = Q(invoice__vat_rate__isnull=False)


def _revenue_aggregates():
    return {
        "total": Sum("revenue_excl", filter=TAXED),
        # Count invoices only: the credit note of a cancelled booking is part of
        # the same booking, not a second document that was left out.
        "untaxed_invoices": Count(
            "invoice", filter=~TAXED & Q(invoice__kind="invoice"), distinct=True
        ),
        # Without a rate, the amount without VAT is the amount including VAT.
        "untaxed_incl": Sum("revenue_excl", filter=~TAXED),
    }


def _night_lines(start, end):
    return InvoiceLine.objects.filter(
        kind=InvoiceLineKind.NIGHT, date__gte=start, date__lt=end
    )


def _for_hotels(queryset, lookup, hotel_ids):
    if hotel_ids is None:
        return queryset
    return queryset.filter(**{f"{lookup}__in": hotel_ids})


def hotel_report(start, end, *, hotel_ids=None):
    """One :class:`HotelRow` per hotel (or per hotel in ``hotel_ids``)."""
    nights = (end - start).days
    hotels = _for_hotels(
        Hotel.objects.select_related("city"), "pk", hotel_ids
    ).order_by("city__name", "name", "pk")
    rows = {
        hotel.pk: HotelRow(hotel_id=hotel.pk, name=hotel.name, city=hotel.city.name)
        for hotel in hotels
    }
    if not rows or nights <= 0:
        return list(rows.values())

    inventories = (
        _for_hotels(Room.objects.all(), "hotel_id", hotel_ids)
        .order_by()
        .values("hotel_id")
        .annotate(total=Sum("inventory"))
        .values_list("hotel_id", "total")
    )
    for hotel_id, inventory in inventories:
        if hotel_id in rows:
            rows[hotel_id].capacity = (inventory or 0) * nights

    blocks = (
        _for_hotels(RoomBlock.objects.all(), "room__hotel_id", hotel_ids)
        .filter(start_date__lt=end, end_date__gt=start)
        .order_by()
        .values_list(
            "room_id",
            "room__hotel_id",
            "room__inventory",
            "start_date",
            "end_date",
            "quantity",
        )
    )
    blocked = defaultdict(int)
    rooms = {}
    for room_id, hotel_id, inventory, block_start, block_end, quantity in blocks:
        rooms[room_id] = (hotel_id, inventory)
        day, last = max(block_start, start), min(block_end, end)
        while day < last:
            blocked[room_id, day] += quantity
            day += ONE_DAY
    for (room_id, _day), quantity in blocked.items():
        hotel_id, inventory = rooms[room_id]
        if hotel_id in rows:
            rows[hotel_id].blocked += min(quantity, inventory)

    booked = (
        _for_hotels(OrderNight.objects.all(), "order__hotel_id", hotel_ids)
        .filter(order__status=OrderStatus.CONFIRMED, date__gte=start, date__lt=end)
        .order_by()
        .values("order__hotel_id")
        .annotate(nights=Count("pk"))
        .values_list("order__hotel_id", "nights")
    )
    for hotel_id, count in booked:
        if hotel_id in rows:
            rows[hotel_id].booked = count

    revenue = (
        _for_hotels(_night_lines(start, end), "invoice__order__hotel_id", hotel_ids)
        .order_by()
        .values("invoice__order__hotel_id")
        .annotate(**_revenue_aggregates())
        .values_list(
            "invoice__order__hotel_id", "total", "untaxed_invoices", "untaxed_incl"
        )
    )
    for hotel_id, total, untaxed_invoices, untaxed_incl in revenue:
        if hotel_id in rows:
            row = rows[hotel_id]
            row.revenue = total or ZERO
            row.untaxed_invoices = untaxed_invoices
            row.untaxed_incl = untaxed_incl or ZERO

    return list(rows.values())


def report_total(rows, *, name=""):
    """The sum of ``rows`` as one row (occupancy over the totals)."""
    total = HotelRow(hotel_id=None, name=name)
    for row in rows:
        total.capacity += row.capacity
        total.blocked += row.blocked
        total.booked += row.booked
        total.revenue += row.revenue
        # An invoice belongs to one hotel: the counts add up.
        total.untaxed_invoices += row.untaxed_invoices
        total.untaxed_incl += row.untaxed_incl
    return total


def revenue_total(start, end):
    """
    The net revenue without VAT of all hotels in ``[start, end)``, as a
    :class:`HotelRow` (only the money fields): one query.
    """
    result = _night_lines(start, end).aggregate(**_revenue_aggregates())
    return HotelRow(
        hotel_id=None,
        name="",
        revenue=result["total"] or ZERO,
        untaxed_invoices=result["untaxed_invoices"],
        untaxed_incl=result["untaxed_incl"] or ZERO,
    )
