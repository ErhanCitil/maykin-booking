"""
The public payment pages: the fake checkout, the return URL and the webhook,
through the test client.
"""
from decimal import Decimal
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse

import requests_mock
from freezegun import freeze_time

from maykinbooking.base.models import EmailLog, OrderStatus, PaymentStatus
from maykinbooking.base.tests.factories import HotelFactory, RoomFactory
from maykinbooking.bookings.services.booking import PaymentMethod

from ..models import (
    FakeProviderPayment,
    Invoice,
    Payment,
    PaymentState,
    ProviderName,
    RefundReason,
)
from ..providers import ProviderError, ProviderNotAvailable
from ..providers.fake import FakeProvider
from ..services.payments import start_online_payment
from .utils import (
    MOLLIE_API,
    MOLLIE_TEST_KEY,
    TempMediaMixin,
    book,
    commit_hooks,
    fake_checkout_path,
    in_days,
    mollie_payment,
    path_of,
    pay_at_provider,
)

SITE_URL = "https://booking.example.com"


@override_settings(MOLLIE_API_KEY="", PAYMENT_FAKE_PROVIDER=True, SITE_URL=SITE_URL)
class PaymentPagesTestCase(TempMediaMixin, TestCase):
    def setUp(self):
        cache.clear()
        self.room = RoomFactory(
            hotel=HotelFactory(is_available=True),
            price=Decimal("100.00"),
            inventory=1,
            is_available=True,
        )

    def start(self):
        order = book(
            self.room, in_days(7), in_days(9), payment_method=PaymentMethod.ONLINE
        )
        return order, start_online_payment(order.pk)

    def return_path(self, payment):
        return reverse("billing:payment_return", args=[payment.reference])

    def assert_private(self, response):
        self.assertEqual(response["X-Robots-Tag"], "noindex")
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        self.assertIn("no-store", response["Cache-Control"])


class FakeFlowTests(PaymentPagesTestCase):
    """The whole online flow through the URLs, as in the browser."""

    def test_book_pay_return_confirmed_with_invoice_and_mail(self):
        # 1. The booking waits for payment; 2. the payment is started.
        order, payment = self.start()
        self.assertEqual(order.status, OrderStatus.PENDING_PAYMENT)
        checkout = path_of(payment.checkout_url)
        self.assertEqual(checkout, fake_checkout_path(payment))

        # 3. The fake checkout page, and "betaald".
        response = self.client.get(checkout)
        self.assertContains(response, "€ 200,00")
        self.assertContains(response, 'value="paid"')
        self.assert_private(response)
        response = self.client.post(checkout, {"action": "paid"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], SITE_URL + self.return_path(payment))

        # 4. Back on the site: the status is asked of the provider.
        with commit_hooks():
            response = self.client.get(path_of(response["Location"]))
        order.refresh_from_db()
        self.assertRedirects(
            response,
            reverse("bookings:detail", args=[order.token]),
            fetch_redirect_response=False,
        )
        self.assert_private(response)

        # 5. Confirmed and paid, with invoice and confirmation mail.
        self.assertEqual(order.status, OrderStatus.CONFIRMED)
        self.assertEqual(order.payment_status, PaymentStatus.PAID)
        invoice = Invoice.objects.get(order=order)
        self.assertTrue(invoice.pdf)
        (confirmation,) = mail.outbox
        self.assertEqual(
            [name for name, _content, _type in confirmation.attachments],
            [f"boeking_{order.pk}.pdf", invoice.filename],
        )
        response = self.client.get(reverse("bookings:detail", args=[order.token]))
        self.assertContains(response, "Je betaling is ontvangen.")

    def test_failed_payment_goes_back_to_the_checkout(self):
        order, payment = self.start()
        response = self.client.post(fake_checkout_path(payment), {"action": "failed"})
        response = self.client.get(path_of(response["Location"]))
        # Back to the checkout of the hotel, with the dates of the booking.
        self.assertRedirects(
            response,
            reverse("order", args=[order.hotel_id])
            + f"?van={order.start_date.isoformat()}&tot={order.end_date.isoformat()}",
            fetch_redirect_response=False,
        )
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.EXPIRED)
        self.assertEqual(order.payment_status, PaymentStatus.UNPAID)
        self.assertFalse(Invoice.objects.exists())
        self.assertEqual(mail.outbox, [])

    def test_failed_second_payment_of_a_booking_that_is_paid_already(self):
        """Review S7: no "boeking niet gemaakt" when another payment paid it."""
        order, first = self.start()
        # A second tab: another payment of the same booking.
        second_remote = FakeProvider().create_payment(
            amount=order.total_price,
            currency="EUR",
            description="tweede",
            redirect_url=SITE_URL + "/",
            webhook_url=None,
            metadata={},
            idempotency_key="second-tab",
        )
        second = Payment.objects.create(
            order=order,
            provider=ProviderName.FAKE,
            provider_id=second_remote.id,
            status=PaymentState.OPEN,
            amount=order.total_price,
        )
        pay_at_provider(first)
        with commit_hooks():
            self.client.get(self.return_path(first))
        pay_at_provider(second, PaymentState.FAILED)

        response = self.client.get(self.return_path(second))

        self.assertRedirects(
            response,
            reverse("bookings:detail", args=[order.token]),
            fetch_redirect_response=False,
        )
        response = self.client.get(response["Location"])
        self.assertContains(response, "je boeking was al betaald")
        self.assertNotContains(response, "boeking is niet gemaakt")
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CONFIRMED)
        self.assertEqual(order.payment_status, PaymentStatus.PAID)

    def test_canceled_payment(self):
        order, payment = self.start()
        response = self.client.post(fake_checkout_path(payment), {"action": "canceled"})
        self.client.get(path_of(response["Location"]))
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.EXPIRED)
        payment.refresh_from_db()
        self.assertEqual(payment.status, PaymentState.CANCELED)


class FakeCheckoutTests(PaymentPagesTestCase):
    def test_unknown_action_or_payment(self):
        order, payment = self.start()
        response = self.client.post(fake_checkout_path(payment), {"action": "rich"})
        self.assertEqual(response.status_code, 400)
        response = self.client.get(
            reverse("billing:fake_checkout", args=["fake_tr_unknown"])
        )
        self.assertEqual(response.status_code, 404)

    def test_paying_twice_changes_nothing(self):
        order, payment = self.start()
        self.client.post(fake_checkout_path(payment), {"action": "failed"})
        response = self.client.post(fake_checkout_path(payment), {"action": "paid"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            FakeProviderPayment.objects.get(pk=payment.provider_id).status, "failed"
        )

    def test_not_found_without_the_fake_provider(self):
        order, payment = self.start()
        with override_settings(PAYMENT_FAKE_PROVIDER=False):
            for method in ("get", "post"):
                with self.subTest(method=method):
                    response = getattr(self.client, method)(
                        fake_checkout_path(payment), {"action": "paid"}
                    )
                    self.assertEqual(response.status_code, 404)
        self.assertEqual(
            FakeProviderPayment.objects.get(pk=payment.provider_id).status, "open"
        )


class ReturnPageTests(PaymentPagesTestCase):
    def test_still_open_shows_a_page_that_reloads(self):
        order, payment = self.start()
        response = self.client.get(self.return_path(payment))
        self.assertContains(response, "Je betaling wordt verwerkt")
        self.assertContains(response, 'http-equiv="refresh"')
        self.assert_private(response)

    def test_provider_error_shows_the_same_page(self):
        order, payment = self.start()
        with mock.patch.object(
            FakeProvider, "get_payment", side_effect=ProviderError("x", retryable=True)
        ):
            response = self.client.get(self.return_path(payment))
        self.assertContains(response, "Je betaling wordt verwerkt")

    def test_late_payment_is_refunded(self):
        with freeze_time("2027-03-01T09:00:00Z"):
            order, payment = self.start()
        with freeze_time("2027-03-01T09:15:30Z"):
            book(self.room, in_days(7), in_days(9))  # the room is taken
        with freeze_time("2027-03-01T09:16:00Z"):
            pay_at_provider(payment)
            with commit_hooks():
                response = self.client.get(self.return_path(payment))
        self.assertContains(response, "Je betaling wordt teruggestort")
        self.assertContains(response, "€ 200,00")
        payment.refresh_from_db()
        self.assertEqual(payment.refunds.get().reason, RefundReason.LATE_PAYMENT)

    def test_unknown_reference(self):
        response = self.client.get(
            reverse(
                "billing:payment_return",
                args=["00000000-0000-0000-0000-000000000000"],
            )
        )
        self.assertEqual(response.status_code, 404)

    def test_too_many_requests(self):
        order, payment = self.start()
        with mock.patch("maykinbooking.billing.views.RETURN_LIMIT_PER_MINUTE", 2):
            for _ in range(2):
                self.client.get(self.return_path(payment))
            response = self.client.get(self.return_path(payment))
        self.assertEqual(response.status_code, 429)


class WebhookTests(PaymentPagesTestCase):
    url = reverse("billing:payment_webhook")

    def setUp(self):
        super().setUp()
        # Mollie does not send a CSRF token.
        self.client = Client(enforce_csrf_checks=True)

    def post(self, provider_id):
        return self.client.post(self.url, {"id": provider_id})

    def test_same_webhook_three_times_confirms_once(self):
        order, payment = self.start()
        pay_at_provider(payment)
        for _ in range(3):
            with commit_hooks():
                self.assertEqual(self.post(payment.provider_id).status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CONFIRMED)
        self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(EmailLog.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(
            order.status_changes.filter(new_value=OrderStatus.CONFIRMED).count(), 1
        )

    def test_unknown_id_is_accepted_without_effect(self):
        self.assertEqual(self.post("tr_doesnotexist").status_code, 200)

    def test_invalid_id(self):
        for provider_id in ["", "1 OR 1=1", "tr_../../x", "x" * 100]:
            with self.subTest(provider_id=provider_id):
                self.assertEqual(self.post(provider_id).status_code, 400)

    def test_only_post(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_provider_error_asks_for_a_retry(self):
        order, payment = self.start()
        with mock.patch.object(
            FakeProvider, "get_payment", side_effect=ProviderError("x", retryable=True)
        ):
            self.assertEqual(self.post(payment.provider_id).status_code, 503)
        with mock.patch.object(
            FakeProvider, "get_payment", side_effect=ProviderNotAvailable("off")
        ), self.assertLogs("maykinbooking.billing", "ERROR"):
            self.assertEqual(self.post(payment.provider_id).status_code, 200)

    def test_other_amount_is_not_confirmed(self):
        order, payment = self.start()
        FakeProviderPayment.objects.filter(pk=payment.provider_id).update(
            status="paid", amount=Decimal("0.01")
        )
        with self.assertLogs("maykinbooking.billing", "ERROR"):
            self.assertEqual(self.post(payment.provider_id).status_code, 200)
        order.refresh_from_db()
        self.assertNotEqual(order.status, OrderStatus.CONFIRMED)
        self.assertFalse(Invoice.objects.exists())

    @override_settings(MOLLIE_API_KEY=MOLLIE_TEST_KEY)
    def test_mollie(self):
        order = book(
            self.room, in_days(7), in_days(9), payment_method=PaymentMethod.ONLINE
        )
        with requests_mock.Mocker() as mocker:
            mocker.post(
                f"{MOLLIE_API}/payments",
                json=mollie_payment("tr_mollie1", amount="200.00"),
                status_code=201,
            )
            payment = start_online_payment(order.pk)
        self.assertEqual(payment.provider, ProviderName.MOLLIE)
        self.assertEqual(payment.provider_id, "tr_mollie1")
        body = mocker.last_request.json()
        self.assertEqual(body["webhookUrl"], SITE_URL + self.url)
        self.assertEqual(body["redirectUrl"], SITE_URL + self.return_path(payment))

        with requests_mock.Mocker() as mocker, commit_hooks():
            mocker.get(
                f"{MOLLIE_API}/payments/tr_mollie1",
                json=mollie_payment(
                    "tr_mollie1",
                    status="paid",
                    amount="200.00",
                    paid_at="2026-11-26T10:00:00+00:00",
                ),
            )
            self.assertEqual(self.post("tr_mollie1").status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CONFIRMED)
        self.assertEqual(Payment.objects.get().status, PaymentState.PAID)

    @override_settings(SITE_URL="http://localhost:8000", MOLLIE_API_KEY=MOLLIE_TEST_KEY)
    def test_no_webhook_without_https(self):
        order = book(
            self.room, in_days(7), in_days(9), payment_method=PaymentMethod.ONLINE
        )
        with requests_mock.Mocker() as mocker:
            mocker.post(
                f"{MOLLIE_API}/payments",
                json=mollie_payment("tr_mollie2", amount="200.00"),
                status_code=201,
            )
            start_online_payment(order.pk)
        self.assertNotIn("webhookUrl", mocker.last_request.json())
