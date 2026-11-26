"""
The staff roles and the permission check of the dashboard views. The full
matrix of every dashboard URL and role is in ``dashboard/tests``.
"""
from django.core.exceptions import ImproperlyConfigured
from django.test import RequestFactory, TestCase
from django.urls import URLResolver, get_resolver, reverse

from maykinbooking.base.tests.factories import OrderFactory
from maykinbooking.dashboard.views import StaffRequiredMixin

from ..roles import MANAGEMENT, RECEPTION, ROLE_PERMISSIONS, role_group
from .factories import (
    ManagerUserFactory,
    ReceptionUserFactory,
    RolelessStaffUserFactory,
    SuperUserFactory,
    UserFactory,
)


def dashboard_views():
    resolver = next(
        pattern
        for pattern in get_resolver().url_patterns
        if isinstance(pattern, URLResolver) and pattern.namespace == "dashboard"
    )
    return [pattern.callback.view_class for pattern in resolver.url_patterns]


class RoleTests(TestCase):
    def test_role_groups_get_exactly_their_permissions(self):
        for name in (RECEPTION, MANAGEMENT):
            with self.subTest(name=name):
                group = role_group(name)
                self.assertEqual(
                    {
                        f"{app_label}.{codename}"
                        for app_label, codename in group.permissions.values_list(
                            "content_type__app_label", "codename"
                        )
                    },
                    set(ROLE_PERMISSIONS[name]),
                )

    def test_the_migration_has_the_same_permissions(self):
        # A change in ROLE_PERMISSIONS needs a new data migration.
        from importlib import import_module

        migration = import_module("maykinbooking.accounts.migrations.0002_staff_roles")
        self.assertEqual(
            {name: set(perms) for name, perms in migration.ROLE_PERMISSIONS.items()},
            {name: set(perms) for name, perms in ROLE_PERMISSIONS.items()},
        )

    def test_factories(self):
        self.assertTrue(ManagerUserFactory().has_perm("base.change_hotel"))
        reception = ReceptionUserFactory()
        self.assertTrue(reception.has_perm("base.cancel_order"))
        self.assertFalse(reception.has_perm("base.change_hotel"))
        roleless = RolelessStaffUserFactory()
        self.assertTrue(roleless.is_staff)
        self.assertFalse(roleless.has_perm("base.view_order"))


class DashboardPermissionTests(TestCase):
    def test_every_dashboard_view_sets_its_permissions(self):
        views = dashboard_views()
        self.assertTrue(views)
        for view in views:
            with self.subTest(view=view.__name__):
                self.assertTrue(issubclass(view, StaffRequiredMixin))
                self.assertIsNotNone(view.permission_required)

    def test_a_view_without_permissions_is_a_configuration_error(self):
        class Forgotten(StaffRequiredMixin):
            pass

        view = Forgotten()
        view.request = RequestFactory().get("/")
        view.request.user = SuperUserFactory()
        with self.assertRaises(ImproperlyConfigured):
            view.has_permission()

    def test_who_can_see_the_bookings(self):
        order = OrderFactory()
        url = reverse("dashboard:booking_detail", args=[order.pk])
        for user, status in [
            (UserFactory(), 403),
            (RolelessStaffUserFactory(), 403),
            (ReceptionUserFactory(), 200),
            (ManagerUserFactory(), 200),
            (SuperUserFactory(), 200),
            (ManagerUserFactory(is_active=False), 302),
        ]:
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertEqual(self.client.get(url).status_code, status)
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)

    def test_reception_cannot_change_hotels_and_starts_at_the_bookings(self):
        order = OrderFactory()
        self.client.force_login(ReceptionUserFactory())
        response = self.client.get(reverse("dashboard:home"))
        self.assertRedirects(
            response, reverse("dashboard:booking_list"), fetch_redirect_response=False
        )
        response = self.client.get(
            reverse("dashboard:hotel_edit", args=[order.hotel_id])
        )
        self.assertEqual(response.status_code, 403)

    def test_staff_without_a_role_sees_the_home_page_only(self):
        self.client.force_login(RolelessStaffUserFactory())
        self.assertEqual(self.client.get(reverse("dashboard:home")).status_code, 200)
        for name in ("booking_list", "hotel_create", "city_create"):
            with self.subTest(name=name):
                response = self.client.get(reverse(f"dashboard:{name}"))
                self.assertEqual(response.status_code, 403)
