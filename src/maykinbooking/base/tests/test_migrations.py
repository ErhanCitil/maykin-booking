"""
Tests for the data migration 0011_backfill_booking_snapshot.

The database is migrated back to 0010, filled with the historical models, and
migrated forward again. Afterwards the test migrates to the latest state.
"""
import re
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

BEFORE = [("base", "0010_booking_foundation")]
AFTER = [("base", "0011_backfill_booking_snapshot")]


class BackfillBookingSnapshotTests(TransactionTestCase):
    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def setUp(self):
        apps = self.migrate(BEFORE)
        City = apps.get_model("base", "City")
        Hotel = apps.get_model("base", "Hotel")
        Room = apps.get_model("base", "Room")
        self.Order = apps.get_model("base", "Order")

        city = City.objects.create(city_id="AMS", name="Amsterdam")
        hotel = Hotel.objects.create(city=city, hotel_id="AMS01", name="Ibis")
        self.room = Room.objects.create(
            hotel=hotel, room_type="Double", price=Decimal("80.50"), description="x"
        )
        self.order_fields = {
            "hotel": hotel,
            "room": self.room,
            "first_name": "Alex",
            "last_name": "de Landgraaf",
            "email": "alex@example.com",
        }

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_order_gets_nights_total_and_log(self):
        created_at = datetime(2026, 11, 1, 12, 0, tzinfo=dt_timezone.utc)
        order = self.Order.objects.create(
            start_date=date(2027, 1, 30), end_date=date(2027, 2, 2), **self.order_fields
        )
        self.Order.objects.filter(pk=order.pk).update(created_at=created_at)

        apps = self.migrate(AFTER)
        Order = apps.get_model("base", "Order")
        OrderStatusChange = apps.get_model("base", "OrderStatusChange")

        order = Order.objects.get(pk=order.pk)
        self.assertEqual(order.status, "confirmed")
        self.assertEqual(order.payment_status, "at_hotel")
        self.assertEqual(order.total_price, Decimal("241.50"))
        self.assertEqual(
            list(order.nights.order_by("date").values_list("date", "price")),
            [
                (date(2027, 1, 30), Decimal("80.50")),
                (date(2027, 1, 31), Decimal("80.50")),
                (date(2027, 2, 1), Decimal("80.50")),
            ],
        )
        changes = OrderStatusChange.objects.filter(order=order).order_by("field")
        self.assertEqual(
            [
                (c.field, c.old_value, c.new_value, c.actor_type, c.actor_user_id)
                for c in changes
            ],
            [
                ("payment_status", "", "at_hotel", "system", None),
                ("status", "", "confirmed", "system", None),
            ],
        )
        for change in changes:
            self.assertEqual(change.reason, "Migratie fase 1")
            self.assertEqual(change.created_at, created_at)

    def test_reverse_removes_only_the_backfill(self):
        migrated = self.Order.objects.create(
            start_date=date(2027, 1, 1), end_date=date(2027, 1, 3), **self.order_fields
        )
        apps = self.migrate(AFTER)
        Order = apps.get_model("base", "Order")
        OrderNight = apps.get_model("base", "OrderNight")
        OrderStatusChange = apps.get_model("base", "OrderStatusChange")

        # Made after the migration: a booking with its own nights and log, and a
        # cancellation of the migrated order.
        later = Order.objects.create(
            start_date=date(2027, 2, 1),
            end_date=date(2027, 2, 2),
            total_price=Decimal("80.50"),
            hotel_id=migrated.hotel_id,
            room_id=migrated.room_id,
            first_name="Sam",
        )
        OrderNight.objects.create(order=later, date=date(2027, 2, 1), price=80.5)
        for order, old, new in [
            (later, "", "confirmed"),
            (migrated, "confirmed", "cancelled"),
        ]:
            OrderStatusChange.objects.create(
                order_id=order.pk,
                field="status",
                old_value=old,
                new_value=new,
                actor_type="guest",
                actor_label="Gast",
            )

        apps = self.migrate(BEFORE)
        Order = apps.get_model("base", "Order")
        OrderNight = apps.get_model("base", "OrderNight")
        OrderStatusChange = apps.get_model("base", "OrderStatusChange")

        self.assertIsNone(Order.objects.get(pk=migrated.pk).total_price)
        self.assertEqual(Order.objects.get(pk=later.pk).total_price, Decimal("80.50"))
        self.assertEqual(
            list(OrderNight.objects.values_list("order_id", "date")),
            [(later.pk, date(2027, 2, 1))],
        )
        self.assertEqual(
            sorted(OrderStatusChange.objects.values_list("order_id", "new_value")),
            sorted([(later.pk, "confirmed"), (migrated.pk, "cancelled")]),
        )
        self.assertFalse(
            OrderStatusChange.objects.filter(reason="Migratie fase 1").exists()
        )

        # Forward again: only the migrated order is filled in again.
        apps = self.migrate(AFTER)
        OrderNight = apps.get_model("base", "OrderNight")
        self.assertEqual(OrderNight.objects.filter(order_id=later.pk).count(), 1)
        self.assertEqual(OrderNight.objects.filter(order_id=migrated.pk).count(), 2)
        self.assertEqual(
            apps.get_model("base", "Order").objects.get(pk=migrated.pk).total_price,
            Decimal("161.00"),
        )

    def test_order_without_valid_period_stops_the_migration(self):
        valid = self.Order.objects.create(
            start_date=date(2027, 1, 1), end_date=date(2027, 1, 3), **self.order_fields
        )
        invalid = [
            self.Order.objects.create(
                start_date=None, end_date=None, **self.order_fields
            ),
            self.Order.objects.create(
                start_date=date(2027, 1, 3),
                end_date=date(2027, 1, 3),
                **self.order_fields,
            ),
        ]

        with self.assertRaises(RuntimeError) as error:
            self.migrate(AFTER)

        ids = re.search(r": ([\d, ]+)\.", str(error.exception)).group(1)
        self.assertEqual(ids, ", ".join(str(order.pk) for order in invalid))
        # Nothing was changed: the migration runs in a transaction.
        self.assertIsNone(self.Order.objects.get(pk=valid.pk).total_price)
        # Remove them, so tearDown can migrate forward again.
        self.Order.objects.filter(pk__in=[order.pk for order in invalid]).delete()
