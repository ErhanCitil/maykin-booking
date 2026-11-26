"""
Migrations 0009-0012 on a realistic set of existing data (testpunt 14).

The database is migrated back to 0008 (before phase 1) and filled with the
historical models like the data of a running site: several cities, hotels, room
types and orders, overlapping orders, a leap day, a month boundary, a stay longer
than the 30 nights new bookings allow, an order without ``created_at`` and a room
of 0 euro. Then everything is migrated to the latest state.
"""
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models import ProtectedError
from django.test import TransactionTestCase
from django.utils import timezone

BEFORE_PHASE_1 = [("base", "0008_hotel_address")]

CREATED_AT = datetime(2026, 10, 1, 12, 30, tzinfo=dt_timezone.utc)

# name: (room, start, end, created_at)
ORDERS = {
    "month boundary": ("ibis_double", date(2027, 1, 30), date(2027, 2, 2), True),
    "overlaps": ("ibis_double", date(2027, 1, 31), date(2027, 2, 1), True),
    "leap day": ("ibis_single", date(2028, 2, 28), date(2028, 3, 1), True),
    "30 nights": ("pulitzer_family", date(2026, 12, 1), date(2026, 12, 31), True),
    "73 nights": ("pulitzer_family", date(2027, 1, 1), date(2027, 3, 15), True),
    "free room": ("dom_double", date(2027, 5, 1), date(2027, 5, 3), True),
    "no created_at": ("ibis_double", date(2025, 1, 1), date(2025, 1, 2), False),
}

PRICES = {
    "ibis_double": Decimal("80.50"),
    "ibis_single": Decimal("65.00"),
    "pulitzer_family": Decimal("1234.99"),
    "dom_double": Decimal("0.00"),
}


class Phase1MigrationsTests(TransactionTestCase):
    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def migrate_to_latest(self):
        executor = MigrationExecutor(connection)
        targets = executor.loader.graph.leaf_nodes()
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def setUp(self):
        apps = self.migrate(BEFORE_PHASE_1)
        City = apps.get_model("base", "City")
        Hotel = apps.get_model("base", "Hotel")
        Room = apps.get_model("base", "Room")
        Order = apps.get_model("base", "Order")

        amsterdam = City.objects.create(city_id="AMS", name="Amsterdam")
        utrecht = City.objects.create(city_id="UTR", name="Utrecht")
        ibis = Hotel.objects.create(city=amsterdam, hotel_id="AMS01", name="Ibis")
        pulitzer = Hotel.objects.create(
            city=amsterdam, hotel_id="AMS02", name="Pulitzer"
        )
        dom = Hotel.objects.create(city=utrecht, hotel_id="UTR01", name="Dom")
        hotels = {"ibis": ibis, "pulitzer": pulitzer, "dom": dom}
        self.rooms = {}
        for key, price in PRICES.items():
            hotel, room_type = key.split("_")
            self.rooms[key] = Room.objects.create(
                hotel=hotels[hotel],
                room_type=room_type.capitalize(),
                price=price,
                description="Kamer",
            )

        self.orders = {}
        for number, (name, (room, start, end, has_created_at)) in enumerate(
            ORDERS.items()
        ):
            order = Order.objects.create(
                hotel=self.rooms[room].hotel,
                room=self.rooms[room],
                start_date=start,
                end_date=end,
                first_name="Zoë",
                last_name=f"Gast {number}",
                email=f"gast{number}@example.com",
                address="Keizersgracht 117",
                zipcode="1015CJ",
            )
            Order.objects.filter(pk=order.pk).update(
                created_at=CREATED_AT + timedelta(hours=number)
                if has_created_at
                else None
            )
            self.orders[name] = order
        self.tokens = {name: order.token for name, order in self.orders.items()}

    def tearDown(self):
        self.migrate_to_latest()

    def snapshot(self, apps):
        """Per order: status, payment status, total, nights and log."""
        Order = apps.get_model("base", "Order")
        result = {}
        for name, before in self.orders.items():
            order = Order.objects.get(pk=before.pk)
            result[name] = {
                "status": order.status,
                "payment_status": order.payment_status,
                "total_price": order.total_price,
                "nights": list(
                    order.nights.order_by("date").values_list("date", "price")
                ),
                "log": list(
                    order.status_changes.order_by("field").values_list(
                        "field",
                        "old_value",
                        "new_value",
                        "actor_type",
                        "actor_user",
                        "actor_label",
                        "reason",
                    )
                ),
            }
        return result

    def test_every_order_gets_status_nights_total_and_log(self):
        started = timezone.now()
        apps = self.migrate_to_latest()
        Order = apps.get_model("base", "Order")
        Room = apps.get_model("base", "Room")

        for name, (room, start, end, has_created_at) in ORDERS.items():
            with self.subTest(name):
                order = Order.objects.get(pk=self.orders[name].pk)
                price = PRICES[room]
                nights = (end - start).days
                self.assertEqual(order.token, self.tokens[name])
                self.assertEqual(order.status, "confirmed")
                self.assertEqual(order.payment_status, "at_hotel")
                self.assertIsNone(order.hold_expires_at)
                self.assertEqual(order.language, "nl")
                self.assertEqual(order.total_price, price * nights)
                self.assertEqual(
                    list(order.nights.order_by("date").values_list("date", "price")),
                    [(start + timedelta(days=i), price) for i in range(nights)],
                )
                changes = order.status_changes.order_by("field")
                self.assertEqual(
                    [
                        (c.field, c.old_value, c.new_value, c.actor_type, c.reason)
                        for c in changes
                    ],
                    [
                        ("payment_status", "", "at_hotel", "system", "Migratie fase 1"),
                        ("status", "", "confirmed", "system", "Migratie fase 1"),
                    ],
                )
                for change in changes:
                    self.assertIsNone(change.actor_user_id)
                    self.assertEqual(change.actor_label, "Systeem")
                    if has_created_at:
                        self.assertEqual(change.created_at, order.created_at)
                    else:
                        self.assertGreaterEqual(change.created_at, started)

        leap = Order.objects.get(pk=self.orders["leap day"].pk)
        self.assertEqual(
            list(leap.nights.values_list("date", flat=True)),
            [date(2028, 2, 28), date(2028, 2, 29)],
        )
        self.assertEqual(
            Order.objects.get(pk=self.orders["73 nights"].pk).nights.count(), 73
        )
        self.assertEqual(
            apps.get_model("base", "OrderNight").objects.count(),
            sum((end - start).days for _, start, end, _ in ORDERS.values()),
        )
        self.assertEqual(
            apps.get_model("base", "OrderStatusChange").objects.count(),
            2 * len(ORDERS),
        )
        self.assertEqual(set(Room.objects.values_list("inventory", flat=True)), {5})

    def test_migrated_orders_occupy_their_nights(self):
        self.migrate_to_latest()
        # The real models and services, now that the schema is the latest one.
        from maykinbooking.base.models import Room
        from maykinbooking.bookings.services.availability import occupancy

        room = Room.objects.get(pk=self.rooms["ibis_double"].pk)
        self.assertEqual(
            occupancy([room.pk], date(2027, 1, 29), date(2027, 2, 3))[room.pk],
            {date(2027, 1, 30): 1, date(2027, 1, 31): 2, date(2027, 2, 1): 1},
        )

    def test_constraints_and_protection_hold_after_the_migration(self):
        self.migrate_to_latest()
        from maykinbooking.base.models import Hotel, Order, Room

        order = Order.objects.get(pk=self.orders["month boundary"].pk)
        for field, value in [
            ("end_date", order.start_date),
            ("status", "bestaat-niet"),
            ("payment_status", "bestaat-niet"),
        ]:
            with self.subTest(field):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    Order.objects.filter(pk=order.pk).update(**{field: value})
        with self.assertRaises(ProtectedError):
            Hotel.objects.get(pk=order.hotel_id).delete()
        with self.assertRaises(ProtectedError):
            Room.objects.get(pk=order.room_id).delete()

    def test_back_to_before_phase_1_and_forward_again(self):
        first = self.snapshot(self.migrate_to_latest())

        apps = self.migrate(BEFORE_PHASE_1)
        Order = apps.get_model("base", "Order")
        self.assertEqual(Order.objects.count(), len(ORDERS))
        self.assertEqual(
            dict(Order.objects.values_list("pk", "end_date")),
            {order.pk: ORDERS[name][2] for name, order in self.orders.items()},
        )

        second = self.snapshot(self.migrate_to_latest())
        for name in ORDERS:
            with self.subTest(name):
                self.assertEqual(second[name], first[name])
