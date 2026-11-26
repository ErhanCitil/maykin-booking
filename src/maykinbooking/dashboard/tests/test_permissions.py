"""
Who may open which dashboard page: every URL of ``dashboard.urls`` for an
anonymous visitor, a user without staff status, staff without a role, the roles
"Receptie" and "Beheer", and the superuser (design, "Rollen en permissies").

Pages are requested with GET, so nothing changes: a page that only takes POST
answers 405 to whoever is let through. POST is checked separately for everyone
who is refused.
"""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import URLResolver, get_resolver, reverse
from django.utils import timezone

from maykinbooking.accounts.tests.factories import (
    ManagerUserFactory,
    ReceptionUserFactory,
    RolelessStaffUserFactory,
    SuperUserFactory,
    UserFactory,
)
from maykinbooking.base.tests.factories import (
    CityFactory,
    HotelFactory,
    OrderFactory,
    RoomBlockFactory,
    RoomFactory,
)
from maykinbooking.billing.models import DiscountCode, SeasonPeriod
from maykinbooking.billing.tests.factories import (
    DiscountCodeFactory,
    SeasonPeriodFactory,
)
from maykinbooking.billing.tests.utils import TempMediaMixin, book, in_days

from .. import urls as dashboard_urls
from ..views import StaffRequiredMixin

# The permissions of every view (design, the table in "Rollen en permissies").
EXPECTED_PERMISSIONS = {
    "home": (),
    "hotel_create": ("base.add_hotel",),
    "hotel_edit": ("base.change_hotel",),
    "hotel_delete": ("base.delete_hotel",),
    "room_create": ("base.add_room",),
    "room_edit": ("base.change_room",),
    "room_delete": ("base.delete_room",),
    "room_blocks": ("base.add_roomblock",),
    "room_block_delete": ("base.delete_roomblock",),
    "city_create": ("base.add_city",),
    "booking_list": ("base.view_order",),
    "booking_export": ("base.view_order",),
    "booking_detail": ("base.view_order",),
    "booking_cancel": ("base.cancel_order",),
    "booking_resend": ("base.resend_order_email",),
    "booking_refund_retry": ("base.cancel_order",),
    "pricing_rules": ("billing.change_pricingsettings",),
    "season_create": ("billing.add_seasonperiod",),
    "season_edit": ("billing.change_seasonperiod",),
    "season_delete": ("billing.delete_seasonperiod",),
    "discount_list": ("billing.view_discountcode",),
    "discount_create": ("billing.add_discountcode",),
    "discount_edit": ("billing.change_discountcode",),
    "invoice_list": ("billing.view_invoice",),
    "invoice_export": ("billing.view_invoice",),
    "invoice_pdf": ("billing.view_invoice",),
    "customer_list": ("base.view_customers",),
    "customer_export": ("base.view_customers",),
    "customer_detail": ("base.view_customers",),
    "report": ("base.view_reports",),
}

# The pages of the reception; everything else (but the home page) is "Beheer".
RECEPTION_PAGES = {
    "booking_list",
    "booking_export",
    "booking_detail",
    "booking_cancel",
    "booking_resend",
    "booking_refund_retry",
}

# Pages that only take POST: a GET by someone who is let through is a 405.
POST_ONLY = {
    "room_block_delete",
    "booking_resend",
    "booking_refund_retry",
    "season_delete",
}


def dashboard_patterns():
    resolver = next(
        pattern
        for pattern in get_resolver().url_patterns
        if isinstance(pattern, URLResolver) and pattern.namespace == "dashboard"
    )
    return resolver.url_patterns


class PermissionMatrixTests(TempMediaMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.users = {
            "non_staff": UserFactory(),
            "roleless": RolelessStaffUserFactory(),
            "reception": ReceptionUserFactory(),
            "manager": ManagerUserFactory(),
            "superuser": SuperUserFactory(),
        }
        hotel = HotelFactory(city=CityFactory(), is_available=True)
        cls.room = RoomFactory(
            hotel=hotel, price=Decimal("100.00"), is_available=True, inventory=3
        )
        # Booked through the service: confirmed, with an invoice.
        cls.order = book(cls.room, in_days(7), in_days(9))
        cls.invoice = cls.order.invoices.get()
        cls.block = RoomBlockFactory(room=cls.room)
        cls.season = SeasonPeriodFactory()
        cls.code = DiscountCodeFactory()

    def urls(self):
        """``{name: url}`` of every dashboard page, with objects that exist."""
        pk = {
            "hotel_edit": self.room.hotel_id,
            "hotel_delete": self.room.hotel_id,
            "room_create": self.room.hotel_id,
            "room_edit": self.room.pk,
            "room_delete": self.room.pk,
            "room_blocks": self.room.pk,
            "room_block_delete": self.block.pk,
            "booking_detail": self.order.pk,
            "booking_cancel": self.order.pk,
            "booking_resend": self.order.pk,
            "booking_refund_retry": self.order.pk,
            "season_edit": self.season.pk,
            "season_delete": self.season.pk,
            "discount_edit": self.code.pk,
            "invoice_pdf": self.invoice.pk,
            "customer_detail": self.order.pk,
        }
        return {
            name: reverse(f"dashboard:{name}", args=[pk[name]] if name in pk else [])
            for name in EXPECTED_PERMISSIONS
        }

    def expected(self, role, name):
        """The status code of a GET of the page ``name`` for ``role``."""
        allowed = 405 if name in POST_ONLY else 200
        if role in ("manager", "superuser"):
            return allowed
        if role == "reception":
            if name == "home":
                return 302  # to the bookings
            return allowed if name in RECEPTION_PAGES else 403
        if role == "roleless":
            return 200 if name == "home" else 403
        return 403

    def test_the_matrix_has_every_dashboard_url(self):
        names = {pattern.name for pattern in dashboard_patterns()}
        self.assertEqual(names, set(EXPECTED_PERMISSIONS))

    def test_anonymous_visitors_go_to_the_login_page(self):
        for name, url in self.urls().items():
            for method in ("get", "post"):
                with self.subTest(name=name, method=method):
                    response = getattr(self.client, method)(url)
                    self.assertRedirects(
                        response,
                        f"{reverse('login')}?next={url}",
                        fetch_redirect_response=False,
                    )

    def check_role(self, role):
        self.client.force_login(self.users[role])
        for name, url in self.urls().items():
            with self.subTest(role=role, name=name):
                response = self.client.get(url)
                expected = self.expected(role, name)
                self.assertEqual(response.status_code, expected)
                if expected == 302:
                    self.assertRedirects(
                        response,
                        reverse("dashboard:booking_list"),
                        fetch_redirect_response=False,
                    )
                if expected == 200:
                    # Guest data is never kept in a cache.
                    self.assertIn("no-store", response["Cache-Control"])
                    if name in ("invoice_export", "customer_export", "booking_export"):
                        b"".join(response.streaming_content)

    def test_users_without_staff_status(self):
        self.check_role("non_staff")

    def test_staff_without_a_role(self):
        self.check_role("roleless")

    def test_reception(self):
        self.check_role("reception")

    def test_management(self):
        self.check_role("manager")

    def test_superuser(self):
        self.check_role("superuser")

    def test_inactive_staff_goes_to_the_login_page(self):
        self.client.force_login(ManagerUserFactory(is_active=False))
        url = reverse("dashboard:pricing_rules")
        self.assertRedirects(
            self.client.get(url),
            f"{reverse('login')}?next={url}",
            fetch_redirect_response=False,
        )

    def test_refused_posts_change_nothing(self):
        data = {
            "weekend_percent": "50",
            "long_stay_min_nights": "2",
            "long_stay_percent": "50",
            "name": "Gehackt",
            "start_date": "2030-01-01",
            "end_date": "2030-01-31",
            "percent": "100",
            "code": "GRATIS",
            "kind": "percent",
            "value": "100",
            "valid_from": "2020-01-01",
            "valid_until": "2030-01-01",
            "is_active": "on",
            "reason": "x",
            "confirm": "on",
        }
        for role in ("non_staff", "roleless", "reception"):
            self.client.force_login(self.users[role])
            for name, url in self.urls().items():
                if self.expected(role, name) != 403:
                    continue
                with self.subTest(role=role, name=name):
                    response = self.client.post(url, data)
                    self.assertEqual(response.status_code, 403)

        self.assertTrue(SeasonPeriod.objects.filter(pk=self.season.pk).exists())
        self.assertFalse(SeasonPeriod.objects.filter(name="Gehackt").exists())
        self.assertFalse(DiscountCode.objects.filter(code="GRATIS").exists())
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "confirmed")


class PermissionGuardTests(TestCase):
    """Every view of the dashboard checks its own permissions."""

    def test_every_view_is_a_staff_view_with_its_permissions(self):
        patterns = dashboard_patterns()
        self.assertEqual(len(patterns), len(dashboard_urls.urlpatterns))
        for pattern in patterns:
            view = pattern.callback.view_class
            with self.subTest(name=pattern.name, view=view.__name__):
                self.assertTrue(issubclass(view, StaffRequiredMixin))
                self.assertIsNotNone(view.permission_required)
                self.assertEqual(
                    set(view().get_permission_required()),
                    set(EXPECTED_PERMISSIONS[pattern.name]),
                )


class NavigationTests(TestCase):
    """The navigation only shows the parts of the dashboard the user may open."""

    LINKS = {
        "booking_list": "Boekingen",
        "pricing_rules": "Prijsregels",
        "discount_list": "Kortingscodes",
        "invoice_list": "Facturen",
        "customer_list": "Klanten",
        "report": "Rapportage",
    }

    def nav_links(self, user, page="dashboard:booking_list"):
        self.client.force_login(user)
        response = self.client.get(reverse(page))
        self.assertEqual(response.status_code, 200)
        nav = response.content.decode().split('aria-label="Dashboard"')[1]
        nav = nav.split("</nav>")[0]
        return {
            name
            for name in self.LINKS
            if f'href="{reverse(f"dashboard:{name}")}"' in nav
        }

    def test_reception_sees_the_bookings_only(self):
        self.assertEqual(self.nav_links(ReceptionUserFactory()), {"booking_list"})

    def test_management_and_superuser_see_everything(self):
        for user in (ManagerUserFactory(), SuperUserFactory()):
            with self.subTest(user=user.username):
                self.assertEqual(self.nav_links(user), set(self.LINKS))

    def test_staff_without_a_role_sees_nothing(self):
        user = RolelessStaffUserFactory()
        self.assertEqual(self.nav_links(user, page="dashboard:home"), set())
        response = self.client.get(reverse("dashboard:home"))
        self.assertContains(response, "Nog geen rol")
        self.assertEqual(list(response.context["hotels"]), [])

    def test_the_current_part_is_marked(self):
        self.client.force_login(ManagerUserFactory())
        response = self.client.get(reverse("dashboard:invoice_list"))
        self.assertContains(
            response,
            f'<a href="{reverse("dashboard:invoice_list")}" aria-current="page"',
        )
        self.assertEqual(response.context["nav_section"], "invoices")


class HomeTilesTests(TestCase):
    def test_management_sees_revenue_and_open_refunds(self):
        self.client.force_login(ManagerUserFactory())
        response = self.client.get(reverse("dashboard:home"))
        self.assertContains(response, "Open terugbetalingen")
        self.assertContains(response, f'href="{reverse("dashboard:report")}"')
        self.assertEqual(response.context["month_revenue"], Decimal("0.00"))
        self.assertEqual(response.context["month_untaxed_invoices"], 0)
        self.assertNotContains(response, "zonder btw-tarief")
        self.assertEqual(response.context["open_refunds"], 0)

    def test_staff_with_hotels_only_does_not_see_the_money(self):
        from django.contrib.auth.models import Permission

        user = RolelessStaffUserFactory()
        user.user_permissions.add(
            Permission.objects.get(
                codename="view_hotel", content_type__app_label="base"
            )
        )
        self.client.force_login(user)
        response = self.client.get(reverse("dashboard:home"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("month_revenue", response.context)
        self.assertNotIn("open_refunds", response.context)
        self.assertNotContains(response, "Open terugbetalingen")
        self.assertNotContains(response, "Nieuw hotel")

    def test_upcoming_tile_is_unchanged(self):
        today = timezone.localdate()
        OrderFactory(start_date=today + timedelta(days=1))
        self.client.force_login(ManagerUserFactory())
        response = self.client.get(reverse("dashboard:home"))
        self.assertEqual(response.context["upcoming_arrivals"], 1)
