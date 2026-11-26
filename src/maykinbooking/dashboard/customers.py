"""
The customers of the dashboard.

There is no customer model: a customer is an e-mail address, trimmed and in
lower case, over all bookings (``EMAIL_KEY``, indexed as
``base_order_email_key_idx``). Only confirmed and cancelled bookings count;
bookings that expired or still wait for payment do not.

The list takes three queries whatever the number of customers: the count of the
paginator, the page (one ``GROUP BY``) and the names (:func:`attach_names`). The
key is always computed by the database, never in Python, so ``LOWER`` and
``TRIM`` mean the same everywhere. A customer page is reached through the
number of one of their bookings: an e-mail address never goes into a URL.
"""
from decimal import Decimal

from django.db.models import Count, DecimalField, F, Max, Q, Sum, Value
from django.db.models.functions import Coalesce, Lower, Trim
from django.utils import timezone

from maykinbooking.base.models import Order, OrderStatus

EMAIL_KEY = Lower(Trim("email"))

CUSTOMER_STATUSES = (OrderStatus.CONFIRMED, OrderStatus.CANCELLED)

# The values of ``?sort=``; anything else is the default. Ties are broken on the
# e-mail address, so the order of a page is stable.
SORTS = {
    "email": (F("key").asc(),),
    "-email": (F("key").desc(),),
    "boekingen": (F("bookings").asc(),),
    "-boekingen": (F("bookings").desc(),),
    "annuleringen": (F("cancellations").asc(),),
    "-annuleringen": (F("cancellations").desc(),),
    "besteed": (F("spent").asc(),),
    "-besteed": (F("spent").desc(),),
    "laatste_verblijf": (F("last_stay").asc(nulls_first=True),),
    "-laatste_verblijf": (F("last_stay").desc(nulls_last=True),),
    "laatste_boeking": (F("last_order_id").asc(),),
    "-laatste_boeking": (F("last_order_id").desc(),),
}
DEFAULT_SORT = "-laatste_boeking"

MONEY = DecimalField(max_digits=12, decimal_places=2)


def search_q(query):
    """
    A booking number (``1234`` or ``#1234``) finds that booking. Other text finds
    the orders whose guest name or e-mail address contains every word.
    """
    query = query.strip().removeprefix("#")
    if query.isdecimal():
        number = int(query)
        # Larger numbers do not fit the id column (and SQLite raises on them).
        return Q(pk=number) if number < 2**31 else Q(pk__in=[])
    condition = Q()
    for word in query.split():
        condition &= (
            Q(first_name__icontains=word)
            | Q(last_name__icontains=word)
            | Q(email__icontains=word)
        )
    return condition


def valid_sort(sort):
    """``sort`` if it is one of :data:`SORTS`, else the default."""
    return sort if sort in SORTS else DEFAULT_SORT


def customer_orders_by_key(keys):
    """The orders of the customers whose key is in ``keys`` (a values query)."""
    return Order.objects.annotate(key=EMAIL_KEY).filter(key__in=keys)


def customers(*, q="", sort=DEFAULT_SORT, keys=None, today=None):
    """
    One row per customer: ``key`` (the e-mail address), ``bookings``,
    ``cancellations``, ``spent`` (the total of the confirmed bookings),
    ``last_stay`` (the last arrival up to today of a confirmed booking, or
    ``None``) and ``last_order_id`` (the newest booking, for the name and the
    link).

    A search selects customers, not bookings: a customer with one booking that
    matches ``q`` is shown with all their bookings in the counts.
    """
    today = today or timezone.localdate()
    orders = Order.objects.filter(status__in=CUSTOMER_STATUSES).annotate(key=EMAIL_KEY)
    if q.strip():
        matching = (
            Order.objects.filter(status__in=CUSTOMER_STATUSES)
            .filter(search_q(q))
            .annotate(key=EMAIL_KEY)
            .values("key")
        )
        orders = orders.filter(key__in=matching)
    if keys is not None:
        orders = orders.filter(key__in=keys)
    return (
        orders.values("key")
        .annotate(
            bookings=Count("pk"),
            cancellations=Count("pk", filter=Q(status=OrderStatus.CANCELLED)),
            spent=Coalesce(
                Sum("total_price", filter=Q(status=OrderStatus.CONFIRMED)),
                Value(Decimal("0.00")),
                output_field=MONEY,
            ),
            last_stay=Max(
                "start_date",
                filter=Q(status=OrderStatus.CONFIRMED, start_date__lte=today),
            ),
            last_order_id=Max("pk"),
        )
        .order_by(*SORTS[valid_sort(sort)], "key")
    )


def attach_names(rows):
    """
    Give every row of :func:`customers` the ``name`` of the guest of its newest
    booking, in one query. Returns the rows as a list.
    """
    rows = list(rows)
    names = {
        pk: f"{first_name} {last_name}".strip()
        for pk, first_name, last_name in Order.objects.filter(
            pk__in=[row["last_order_id"] for row in rows]
        ).values_list("pk", "first_name", "last_name")
    }
    for row in rows:
        row["name"] = names.get(row["last_order_id"], "")
    return rows


def key_of(order_pk):
    """The customer key of the order ``order_pk``, as a subquery."""
    return Order.objects.filter(pk=order_pk).annotate(key=EMAIL_KEY).values("key")


def customer_orders(order):
    """All bookings, in every status, of the customer of ``order``."""
    return customer_orders_by_key(key_of(order.pk))


def customer_summary(order, *, today=None):
    """The row of :func:`customers` of the customer of ``order``, or ``None``."""
    rows = customers(keys=key_of(order.pk), today=today)
    return rows.first()
