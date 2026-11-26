"""
Give the existing orders the snapshot that new bookings get from the booking service.

Per order: one ``OrderNight`` per night at the current price of the room, the total
price, and two ``OrderStatusChange`` rows (status and payment status, by the system).
Only orders without a total price are filled in, so running it again after a
reverse leaves the bookings made in the meantime alone.
Migration 0012 makes the dates and the total price NOT NULL. It is a separate
migration because PostgreSQL does not allow ALTER TABLE on a table with pending
trigger events in the same transaction.
"""
from datetime import timedelta

from django.db import migrations
from django.db.models import F, Q
from django.utils import timezone

REASON = "Migratie fase 1"
ACTOR_LABEL = "Systeem"
BATCH_SIZE = 500


def backfill(apps, schema_editor):
    Order = apps.get_model("base", "Order")
    OrderNight = apps.get_model("base", "OrderNight")
    OrderStatusChange = apps.get_model("base", "OrderStatusChange")

    invalid = list(
        Order.objects.filter(
            Q(start_date__isnull=True)
            | Q(end_date__isnull=True)
            | Q(end_date__lte=F("start_date"))
        )
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    if invalid:
        raise RuntimeError(
            "Deze orders hebben geen geldige periode (start_date en end_date "
            "ontbreken of end_date <= start_date): {}. Corrigeer of verwijder ze "
            "en draai de migratie opnieuw.".format(", ".join(map(str, invalid)))
        )

    now = timezone.now()
    orders, nights, changes = [], [], []
    to_fill = Order.objects.filter(total_price__isnull=True)
    for order in to_fill.select_related("room").order_by("pk"):
        price = order.room.price
        night_count = (order.end_date - order.start_date).days
        nights.extend(
            OrderNight(
                order=order, date=order.start_date + timedelta(days=i), price=price
            )
            for i in range(night_count)
        )
        order.total_price = price * night_count
        orders.append(order)
        for field in ("status", "payment_status"):
            changes.append(
                OrderStatusChange(
                    order=order,
                    field=field,
                    old_value="",
                    new_value=getattr(order, field),
                    actor_type="system",
                    actor_label=ACTOR_LABEL,
                    reason=REASON,
                    created_at=order.created_at or now,
                )
            )

    Order.objects.bulk_update(orders, ["total_price"], batch_size=BATCH_SIZE)
    OrderNight.objects.bulk_create(nights, batch_size=BATCH_SIZE)
    OrderStatusChange.objects.bulk_create(changes, batch_size=BATCH_SIZE)


def remove_backfill(apps, schema_editor):
    """
    Undo only what ``backfill`` made: its log rows, and the nights and total of
    the orders it filled in. Bookings, nights and log rows made after the
    migration are kept.
    """
    Order = apps.get_model("base", "Order")
    OrderNight = apps.get_model("base", "OrderNight")
    OrderStatusChange = apps.get_model("base", "OrderStatusChange")

    migrated = OrderStatusChange.objects.filter(
        reason=REASON, actor_type="system", old_value=""
    )
    order_ids = list(migrated.values_list("order_id", flat=True).distinct())
    for start in range(0, len(order_ids), BATCH_SIZE):
        batch = order_ids[start : start + BATCH_SIZE]
        OrderNight.objects.filter(order_id__in=batch).delete()
        Order.objects.filter(pk__in=batch).update(total_price=None)
    migrated.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("base", "0010_booking_foundation"),
    ]

    operations = [
        migrations.RunPython(backfill, remove_backfill),
    ]
