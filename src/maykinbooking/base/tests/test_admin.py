from django.test import TestCase
from django.urls import reverse

from maykinbooking.accounts.tests.factories import SuperUserFactory
from maykinbooking.base.models import EmailLog, OrderStatusChange

from .factories import OrderFactory


class OrderAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = SuperUserFactory()
        cls.order = OrderFactory()
        OrderStatusChange.objects.create(
            order=cls.order,
            field="status",
            new_value="confirmed",
            actor_type="system",
            reason="Logregel uit de test",
        )
        EmailLog.objects.create(
            order=cls.order,
            kind="booking_confirmation",
            recipient="gast@example.com",
            subject="Onderwerp uit de test",
            status="failed",
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_change_page_shows_nights_log_and_mails_read_only(self):
        response = self.client.get(
            reverse("admin:base_order_change", args=[self.order.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Logregel uit de test")
        self.assertContains(response, "Onderwerp uit de test")
        self.assertContains(response, "Nacht van")  # column of the nights inline
        # Read-only inlines: no form fields and no rows to add.
        self.assertNotContains(response, 'name="nights-0-date"')
        self.assertNotContains(response, 'name="status_changes-0-reason"')
        self.assertNotContains(response, 'name="emails-0-subject"')
        self.assertNotContains(response, 'name="status"')
        self.assertNotContains(response, 'name="total_price"')

    def test_orders_cannot_be_added_in_the_admin(self):
        response = self.client.get(reverse("admin:base_order_add"))
        self.assertEqual(response.status_code, 403)

    def test_orders_cannot_be_deleted_in_the_admin(self):
        response = self.client.get(
            reverse("admin:base_order_delete", args=[self.order.pk])
        )
        self.assertEqual(response.status_code, 403)
        response = self.client.post(
            reverse("admin:base_order_delete", args=[self.order.pk]), {"post": "yes"}
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(type(self.order).objects.filter(pk=self.order.pk).exists())

        changelist = self.client.get(reverse("admin:base_order_changelist"))
        self.assertEqual(changelist.status_code, 200)
        self.assertNotContains(changelist, 'value="delete_selected"')
