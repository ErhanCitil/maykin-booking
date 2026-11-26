"""
The payment of a booking in the dashboard: the panel "Betaling" with the
payment, the refunds and the invoices, the payment badge in the list, and
trying a failed refund again.
"""
from decimal import Decimal
from unittest import mock

from django.contrib.messages import get_messages
from django.test import TestCase, override_settings
from django.urls import reverse

from maykinbooking.accounts.tests.factories import (
    ManagerUserFactory,
    ReceptionUserFactory,
)
from maykinbooking.base.models import OrderStatus, PaymentStatus
from maykinbooking.base.tests.factories import HotelFactory, RoomFactory
from maykinbooking.billing.models import InvoiceKind, Refund, RefundState
from maykinbooking.billing.providers import ProviderError
from maykinbooking.billing.providers.fake import FakeProvider
from maykinbooking.billing.services.payments import start_online_payment, sync_payment
from maykinbooking.billing.tests.factories import DiscountCodeFactory
from maykinbooking.billing.tests.utils import (
    TempMediaMixin,
    book,
    commit_hooks,
    in_days,
    pay_at_provider,
)
from maykinbooking.bookings.services.booking import Actor, PaymentMethod, cancel_booking

TIMEOUT = ProviderError("Mollie POST /payments/x/refunds: timeout.", retryable=True)


@override_settings(
    MOLLIE_API_KEY="",
    PAYMENT_FAKE_PROVIDER=True,
    SITE_URL="https://booking.example.com",
)
class PaymentPanelTestCase(TempMediaMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = ManagerUserFactory(username="beheer")
        cls.reception = ReceptionUserFactory(username="receptie")
        cls.room = RoomFactory(
            hotel=HotelFactory(is_available=True),
            price=Decimal("100.00"),
            inventory=2,
            is_available=True,
        )

    def setUp(self):
        self.client.force_login(self.staff)

    def paid_order(self):
        order = book(
            self.room, in_days(7), in_days(9), payment_method=PaymentMethod.ONLINE
        )
        payment = start_online_payment(order.pk)
        pay_at_provider(payment)
        with commit_hooks():
            sync_payment(payment.pk)
        order.refresh_from_db()
        return order

    def cancel(self, order, *, failing=False):
        """Cancel ``order``; with ``failing`` the refund (after the commit) fails."""
        if failing:
            with mock.patch.object(
                FakeProvider, "create_refund", side_effect=TIMEOUT
            ), self.assertLogs("maykinbooking.billing", "ERROR"):
                return self.cancel(order)
        with commit_hooks():
            cancel_booking(
                order.pk,
                actor=Actor.staff(self.staff),
                reason="Gast belde",
                enforce_deadline=False,
            )
        order.refresh_from_db()
        return order

    def detail(self, order):
        return self.client.get(reverse("dashboard:booking_detail", args=[order.pk]))

    def retry(self, order):
        return self.client.post(
            reverse("dashboard:booking_refund_retry", args=[order.pk])
        )

    def messages(self, response):
        return [str(message) for message in get_messages(response.wsgi_request)]


class PaymentPanelTests(PaymentPanelTestCase):
    def test_payment_at_the_hotel(self):
        order = book(self.room, in_days(7), in_days(9))
        invoice = order.invoices.get()

        response = self.detail(order)

        self.assertContains(response, "Betaling")
        self.assertContains(response, "In het hotel")
        self.assertContains(response, f"Factuur {invoice.number}")
        self.assertContains(
            response, reverse("dashboard:invoice_pdf", args=[invoice.pk])
        )
        self.assertFalse(response.context["refund_retry_needed"])
        self.assertNotContains(response, "Terugbetaling opnieuw proberen")

    def test_paid_online(self):
        order = self.paid_order()
        payment = order.payments.get()

        response = self.detail(order)

        self.assertEqual(response.context["payment"], payment)
        self.assertContains(response, "Online · iDEAL")
        self.assertContains(response, "Nep-provider")
        self.assertContains(response, "€ 200,00")
        self.assertContains(response, "Betaald")
        self.assertIsNotNone(payment.paid_at)

    def test_cancelled_and_refunded_with_credit_note(self):
        order = self.cancel(self.paid_order())
        credit_note = order.invoices.get(kind=InvoiceKind.CREDIT_NOTE)

        response = self.detail(order)

        self.assertEqual(order.payment_status, PaymentStatus.REFUNDED)
        self.assertContains(response, "Terugbetalingen")
        self.assertContains(response, "Terugbetaald")
        self.assertContains(response, "Annulering")
        self.assertContains(response, f"Creditnota {credit_note.number}")
        self.assertContains(
            response, reverse("dashboard:invoice_pdf", args=[credit_note.pk])
        )
        self.assertNotContains(response, "Terugbetaling opnieuw proberen")
        # Without seller details both are test documents, and both say so.
        self.assertTrue(credit_note.is_test)
        self.assertContains(response, ">Test</span>", count=2)

    def test_failed_refund_shows_error_attempts_and_the_button(self):
        order = self.cancel(self.paid_order(), failing=True)

        response = self.detail(order)

        (refund,) = response.context["refunds"]
        self.assertEqual(refund.status, RefundState.ERROR)
        self.assertTrue(response.context["refund_retry_needed"])
        self.assertContains(response, "Fout")
        self.assertContains(
            response, "ProviderError: Mollie POST /payments/x/refunds: timeout."
        )
        self.assertContains(response, "terugbetaling mislukt")
        self.assertContains(
            response,
            f'action="{reverse("dashboard:booking_refund_retry", args=[order.pk])}"',
        )
        self.assertContains(response, "Terugbetaling opnieuw proberen")

    def test_reception_sees_the_invoices_without_a_download(self):
        order = book(self.room, in_days(7), in_days(9))
        invoice = order.invoices.get()
        self.client.force_login(self.reception)

        response = self.detail(order)

        self.assertContains(response, f"Factuur {invoice.number}")
        self.assertNotContains(
            response, reverse("dashboard:invoice_pdf", args=[invoice.pk])
        )
        self.assertNotContains(response, "/pdf/")
        # The link to "Mijn boeking" stays (for the guest), with a warning that
        # it shows the invoices too.
        self.assertContains(response, f"/boeking/{order.token}/")
        self.assertContains(
            response, "bekijken, annuleren en de facturen downloaden. Deel hem alleen"
        )

    def test_discounts_and_credit_note_in_one_notation(self):
        code = DiscountCodeFactory(code="WELKOM10", value=Decimal("10.00"))
        order = book(self.room, in_days(7), in_days(9), discount_code=code.code)
        order = self.cancel(order)

        response = self.detail(order)

        self.assertContains(response, "Kortingscode WELKOM10")
        self.assertContains(response, "\u2212 € 20,00")  # the discount
        self.assertContains(response, "€ 200,00")  # the subtotal
        self.assertContains(response, "\u2212 € 180,00")  # the credit note
        self.assertNotContains(response, "€ -")

    def test_waiting_for_payment(self):
        order = book(
            self.room, in_days(7), in_days(9), payment_method=PaymentMethod.ONLINE
        )
        start_online_payment(order.pk)

        response = self.detail(order)

        self.assertContains(response, "De factuur volgt zodra de betaling binnen is.")
        self.assertContains(response, "Open")


class PaymentBadgeTests(PaymentPanelTestCase):
    def test_list_shows_payment_status_and_failed_refunds(self):
        at_hotel = book(self.room, in_days(7), in_days(9))
        failed = self.cancel(self.paid_order(), failing=True)

        response = self.client.get(reverse("dashboard:booking_list"))

        self.assertContains(response, "Betalen in het hotel")
        self.assertContains(response, "terugbetaling mislukt", count=1)
        self.assertContains(response, "bg-moss")  # the dot of "Betaald"
        rows = {order.pk: order for order in response.context["orders"]}
        self.assertTrue(rows[failed.pk].refund_failed)
        self.assertFalse(rows[at_hotel.pk].refund_failed)

    def test_quick_filter_open_refunds_and_home_tile(self):
        book(self.room, in_days(7), in_days(9))
        failed = self.cancel(self.paid_order(), failing=True)
        self.cancel(self.paid_order())  # refunded: not open

        response = self.client.get(
            reverse("dashboard:booking_list"), {"terugbetaling": "open"}
        )
        self.assertEqual(
            [order.pk for order in response.context["orders"]], [failed.pk]
        )
        quick = {item["label"]: item for item in response.context["quick_filters"]}
        self.assertTrue(quick["Terugbetaling open"]["active"])

        response = self.client.get(reverse("dashboard:home"))
        self.assertEqual(response.context["open_refunds"], 1)
        self.assertEqual(response.context["failed_refunds"], 1)
        self.assertContains(response, "1 mislukt, probeer opnieuw")
        self.assertContains(
            response, f'{reverse("dashboard:booking_list")}?terugbetaling=open'
        )


class RefundRetryTests(PaymentPanelTestCase):
    def test_retry_refunds_the_guest(self):
        order = self.cancel(self.paid_order(), failing=True)

        with commit_hooks(), self.assertLogs("maykinbooking.dashboard.views", "INFO"):
            response = self.retry(order)

        self.assertRedirects(
            response, reverse("dashboard:booking_detail", args=[order.pk])
        )
        refund = Refund.objects.get()
        self.assertEqual(refund.status, RefundState.REFUNDED)
        self.assertEqual(refund.attempts, 2)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.REFUNDED)
        self.assertEqual(order.status, OrderStatus.CANCELLED)

        response = self.detail(order)
        self.assertContains(response, "Terugbetaald")
        self.assertNotContains(response, "Terugbetaling opnieuw proberen")

    def test_reception_may_retry(self):
        order = self.cancel(self.paid_order(), failing=True)
        self.client.force_login(self.reception)
        with commit_hooks():
            response = self.retry(order)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Refund.objects.get().status, RefundState.REFUNDED)

    def test_failing_again_keeps_the_refund_in_error(self):
        order = self.cancel(self.paid_order(), failing=True)

        with mock.patch.object(
            FakeProvider, "create_refund", side_effect=TIMEOUT
        ), self.assertLogs("maykinbooking.billing", "ERROR"):
            with commit_hooks():
                self.retry(order)

        refund = Refund.objects.get()
        self.assertEqual((refund.status, refund.attempts), (RefundState.ERROR, 2))
        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.PAID)
        self.assertContains(self.detail(order), "Terugbetaling opnieuw proberen")

    def test_nothing_to_refund(self):
        order = book(self.room, in_days(7), in_days(9))

        response = self.retry(order)

        self.assertEqual(
            self.messages(response),
            ["Er hoeft voor deze boeking niets teruggestort te worden."],
        )
        self.assertFalse(Refund.objects.exists())

    def test_messages_follow_the_result(self):
        order = self.cancel(self.paid_order(), failing=True)
        refund = Refund.objects.get()
        target = "maykinbooking.dashboard.views.retry_refund"

        refund.status, refund.attempts = RefundState.REFUNDED, 2
        with mock.patch(target, return_value=refund):
            response = self.retry(order)
        self.assertEqual(
            self.messages(response), ["€ 200,00 is teruggestort naar de gast."]
        )

        refund.status, refund.attempts = RefundState.ERROR, 2
        with mock.patch(target, return_value=refund):
            response = self.retry(order)
        # The messages are not shown in between: the newest is the last.
        self.assertIn("opnieuw mislukt", self.messages(response)[-1])

        # Not tried yet (sent after the commit) or on its way at the provider.
        for status, attempts in ((RefundState.ERROR, 1), (RefundState.PENDING, 2)):
            refund.status, refund.attempts = status, attempts
            with mock.patch(target, return_value=refund):
                response = self.retry(order)
            self.assertIn("is aangevraagd", self.messages(response)[-1])

    def test_only_post(self):
        order = self.cancel(self.paid_order(), failing=True)
        response = self.client.get(
            reverse("dashboard:booking_refund_retry", args=[order.pk])
        )
        self.assertEqual(response.status_code, 405)
        self.assertEqual(Refund.objects.get().status, RefundState.ERROR)

    def test_unknown_booking_is_404(self):
        response = self.client.post(
            reverse("dashboard:booking_refund_retry", args=[999999])
        )
        self.assertEqual(response.status_code, 404)
