"""
The migrations of phase 2 on existing data: base 0014-0016, billing 0001-0002
and accounts 0002 (staff roles).

The database is migrated back to base 0013 (end of phase 1), filled with the
historical models, and migrated forward. Afterwards the test migrates to the
latest state.
"""
from datetime import date
from decimal import Decimal

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

BEFORE_PHASE_2 = [("base", "0013_order_guest_field_validators")]

RECEPTION = {"view_order", "cancel_order", "resend_order_email"}


class Phase2MigrationsTests(TransactionTestCase):
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
        apps = self.migrate(BEFORE_PHASE_2)
        City = apps.get_model("base", "City")
        Hotel = apps.get_model("base", "Hotel")
        Room = apps.get_model("base", "Room")
        Order = apps.get_model("base", "Order")
        OrderNight = apps.get_model("base", "OrderNight")
        User = apps.get_model("accounts", "User")

        city = City.objects.create(city_id="AMS", name="Amsterdam")
        hotel = Hotel.objects.create(city=city, hotel_id="AMS01", name="Ibis")
        room = Room.objects.create(
            hotel=hotel, room_type="Double", price=Decimal("80.50"), description="x"
        )
        self.order = Order.objects.create(
            hotel=hotel,
            room=room,
            start_date=date(2027, 1, 30),
            end_date=date(2027, 2, 1),
            first_name="Alex",
            last_name="de Landgraaf",
            email=" Alex@Example.com",
            address="Keizersgracht 117",
            zipcode="1015CJ",
            total_price=Decimal("161.00"),
        )
        for day, price in [(30, "80.50"), (31, "80.50")]:
            OrderNight.objects.create(
                order=self.order, date=date(2027, 1, day), price=Decimal(price)
            )
        self.staff = User.objects.create(username="medewerker", is_staff=True)
        self.superuser = User.objects.create(
            username="beheerder", is_staff=True, is_superuser=True
        )
        self.guest = User.objects.create(username="gast")

    def tearDown(self):
        self.migrate_to_latest()

    def test_existing_data_after_phase_2(self):
        apps = self.migrate_to_latest()
        Order = apps.get_model("base", "Order")
        Hotel = apps.get_model("base", "Hotel")
        OrderNight = apps.get_model("base", "OrderNight")

        order = Order.objects.get(pk=self.order.pk)
        self.assertEqual(order.total_price, Decimal("161.00"))
        self.assertEqual(
            (
                order.city,
                order.length_discount_percent,
                order.length_discount,
                order.discount_code,
                order.code_discount,
            ),
            ("", Decimal("0"), Decimal("0"), "", Decimal("0")),
        )
        self.assertEqual(Hotel.objects.get().country, "NL")
        self.assertEqual(
            list(
                OrderNight.objects.order_by("date").values_list(
                    "base_price", "weekend_percent", "season_percent", "price"
                )
            ),
            [(Decimal("80.50"), Decimal("0"), Decimal("0"), Decimal("80.50"))] * 2,
        )

        VatRate = apps.get_model("billing", "VatRate")
        self.assertEqual(
            list(
                VatRate.objects.order_by("valid_from").values_list(
                    "country", "rate", "valid_from"
                )
            ),
            [
                ("NL", Decimal("9.00"), date(2019, 1, 1)),
                ("NL", Decimal("21.00"), date(2026, 1, 1)),
            ],
        )
        PricingSettings = apps.get_model("billing", "PricingSettings")
        rules = PricingSettings.objects.get()
        self.assertEqual(
            (rules.pk, rules.weekend_percent, rules.long_stay_percent),
            (1, Decimal("0"), Decimal("0")),
        )

    def test_staff_roles(self):
        apps = self.migrate_to_latest()
        Group = apps.get_model("auth", "Group")
        User = apps.get_model("accounts", "User")

        reception = Group.objects.get(name="Receptie")
        management = Group.objects.get(name="Beheer")
        self.assertEqual(
            set(reception.permissions.values_list("codename", flat=True)), RECEPTION
        )
        management_perms = set(
            management.permissions.values_list("content_type__app_label", "codename")
        )
        self.assertTrue(
            {("base", codename) for codename in RECEPTION} <= management_perms
        )
        for permission in [
            ("base", "change_hotel"),
            ("base", "view_customers"),
            ("base", "view_reports"),
            ("billing", "change_pricingsettings"),
            ("billing", "add_discountcode"),
            ("billing", "view_invoice"),
        ]:
            self.assertIn(permission, management_perms)
        self.assertNotIn(("billing", "change_vatrate"), management_perms)

        # Existing staff keeps the dashboard: "Beheer". Not the superuser (who
        # can do everything anyway) and not other users.
        self.assertEqual(
            list(
                User.objects.get(pk=self.staff.pk).groups.values_list("name", flat=True)
            ),
            ["Beheer"],
        )
        self.assertFalse(User.objects.get(pk=self.superuser.pk).groups.exists())
        self.assertFalse(User.objects.get(pk=self.guest.pk).groups.exists())

    def test_back_to_phase_1_and_forward_again(self):
        self.migrate_to_latest()
        apps = self.migrate(BEFORE_PHASE_2)
        self.assertFalse(apps.get_model("auth", "Group").objects.exists())
        self.assertNotIn("billing_invoice", connection.introspection.table_names())
        OrderNight = apps.get_model("base", "OrderNight")
        self.assertEqual(OrderNight.objects.count(), 2)

        apps = self.migrate_to_latest()
        self.assertEqual(
            set(apps.get_model("auth", "Group").objects.values_list("name", flat=True)),
            {"Receptie", "Beheer"},
        )
        self.assertEqual(
            apps.get_model("base", "OrderNight")
            .objects.filter(base_price=Decimal("80.50"))
            .count(),
            2,
        )

    def test_expired_orders_that_waited_for_payment_become_unpaid(self):
        Order = self.migrate(BEFORE_PHASE_2).get_model("base", "Order")
        Order.objects.filter(pk=self.order.pk).update(
            status="expired", payment_status="open"
        )
        apps = self.migrate_to_latest()
        order = apps.get_model("base", "Order").objects.get(pk=self.order.pk)
        self.assertEqual(order.payment_status, "unpaid")

        # Back: "open" again, before "unpaid" is no longer allowed.
        apps = self.migrate(BEFORE_PHASE_2)
        order = apps.get_model("base", "Order").objects.get(pk=self.order.pk)
        self.assertEqual(order.payment_status, "open")

    def test_the_new_services_work_on_migrated_orders(self):
        self.migrate_to_latest()
        # The real models, now that the schema is the latest one.
        from maykinbooking.base.models import Order
        from maykinbooking.bookings.emails import booking_context

        order = Order.objects.get(pk=self.order.pk)
        context = booking_context(order)
        self.assertIsNone(context["invoice"])
        self.assertIsNone(context["payment"])
        self.assertEqual(context["discount_lines"], [])
        self.assertEqual(context["subtotal"], Decimal("161.00"))
