"""
The payment choice of the checkout (phase 2): paying at the hotel or online,
starting the payment, a confirmation that is sent twice, and a payment that
cannot be started.

The fake provider stands in for Mollie (``PAYMENT_FAKE_PROVIDER``).
"""
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from maykinbooking.base.models import (
    City,
    Hotel,
    Order,
    OrderStatus,
    PaymentStatus,
    Room,
)
from maykinbooking.billing.models import (
    DiscountKind,
    FakeProviderPayment,
    Invoice,
    Payment,
    PaymentState,
)
from maykinbooking.billing.providers import ProviderError
from maykinbooking.billing.providers.fake import FakeProvider
from maykinbooking.billing.tests.factories import DiscountCodeFactory
from maykinbooking.billing.tests.utils import (
    TempMediaMixin,
    fake_checkout_path,
    path_of,
)

from .factories import OrderFactory

SITE_URL = "https://booking.example.com"
GUEST = {
    "first_name": "Alex",
    "last_name": "de Landgraaf",
    "email": "alex@example.com",
    "address": "Keizersgracht 117",
    "zipcode": "1015 CJ",
    "city": "Amsterdam",
    "country": "NL",
}
START_FAILED = "Online betalen is nu niet gelukt"


@override_settings(MOLLIE_API_KEY="", PAYMENT_FAKE_PROVIDER=True, SITE_URL=SITE_URL)
class CheckoutPaymentTestCase(TempMediaMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        city = City.objects.create(city_id="AMS", name="Amsterdam")
        cls.hotel = Hotel.objects.create(
            city=city, hotel_id="AMS01", name="Hotel de Gracht", is_available=True
        )
        cls.room = Room.objects.create(
            hotel=cls.hotel,
            room_type="Double",
            price=Decimal("100"),
            description="Kamer",
            inventory=1,
        )
        cls.url = reverse("order", args=[cls.hotel.pk])

    def setUp(self):
        cache.clear()
        self.start = timezone.localdate() + timedelta(days=7)
        self.end = self.start + timedelta(days=2)

    def post_step(self, step, data):
        payload = {"checkout-current_step": step}
        payload.update({f"{step}-{key}": value for key, value in data.items()})
        return self.client.post(self.url, payload)

    def to_confirm_step(self, **stay):
        self.post_step(
            "stay",
            {
                "start_date": self.start,
                "end_date": self.end,
                "room": self.room.pk,
                **stay,
            },
        )
        return self.post_step("guest", GUEST)

    def confirm(self, payment_method="online", **data):
        return self.post_step(
            "confirm", {"terms": "on", "payment_method": payment_method, **data}
        )

    def session_token(self):
        return self.client.session["wizard_checkout"]["extra_data"]["booking_token"]


class PaymentChoiceTests(CheckoutPaymentTestCase):
    def test_both_ways_are_offered_with_the_hotel_preselected(self):
        response = self.to_confirm_step()
        self.assertContains(response, "Hoe wil je betalen?")
        self.assertContains(response, "In het hotel betalen")
        self.assertContains(response, "Nu online betalen (iDEAL)")
        self.assertContains(response, "We houden de kamer 15 minuten voor je vast")
        self.assertContains(response, 'value="at_hotel" class="h-5')
        form = response.context["form"]
        self.assertEqual(form["payment_method"].value(), "at_hotel")
        self.assertContains(response, "of nu online met iDEAL")

    def test_without_a_choice_the_guest_pays_at_the_hotel(self):
        self.to_confirm_step()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.post_step("confirm", {"terms": "on"})
        order = Order.objects.get()
        self.assertRedirects(
            response,
            reverse("bookings:detail", args=[order.token]),
            fetch_redirect_response=False,
        )
        self.assertEqual(order.status, OrderStatus.CONFIRMED)
        self.assertEqual(order.payment_status, PaymentStatus.AT_HOTEL)
        self.assertFalse(Payment.objects.exists())
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(PAYMENT_FAKE_PROVIDER=False)
    def test_without_a_provider_only_the_hotel(self):
        response = self.to_confirm_step()
        self.assertNotContains(response, 'value="online"')
        self.assertNotContains(response, "iDEAL")
        self.assertContains(response, "Je betaalt in het hotel.")

        response = self.confirm()
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, "Online betalen is voor deze boeking niet mogelijk."
        )
        self.assertFalse(Order.objects.exists())

    def test_nothing_to_pay_is_paid_at_the_hotel(self):
        DiscountCodeFactory(code="GRATIS", kind=DiscountKind.FIXED, value=500)
        response = self.to_confirm_step(discount_code="GRATIS")
        self.assertNotContains(response, 'value="online"')
        response = self.confirm()
        self.assertContains(
            response, "Online betalen is voor deze boeking niet mogelijk."
        )
        self.assertFalse(Order.objects.exists())


class OnlinePaymentTests(CheckoutPaymentTestCase):
    def test_booking_waits_for_payment_and_the_guest_goes_to_the_payment_page(self):
        self.to_confirm_step()
        token = self.session_token()

        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            response = self.confirm()

        order = Order.objects.get()
        payment = Payment.objects.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], payment.checkout_url)
        self.assertEqual(response["Location"], SITE_URL + fake_checkout_path(payment))
        self.assertEqual(order.token.hex, token)
        self.assertEqual(order.status, OrderStatus.PENDING_PAYMENT)
        self.assertEqual(order.payment_status, PaymentStatus.OPEN)
        self.assertAlmostEqual(
            order.hold_expires_at,
            timezone.now() + timedelta(minutes=15),
            delta=timedelta(seconds=30),
        )
        self.assertEqual(payment.order, order)
        self.assertEqual(payment.status, PaymentState.OPEN)
        self.assertEqual(payment.amount, Decimal("200.00"))
        # No mail and no invoice before the payment.
        self.assertEqual(callbacks, [])
        self.assertEqual(mail.outbox, [])
        self.assertFalse(Invoice.objects.exists())
        # The checkout is done: a new one starts at step 1.
        self.assertNotIn("booking_token", self.client.session["wizard_checkout"])

    def test_discount_code_and_expected_total_are_passed_on(self):
        DiscountCodeFactory(code="WELKOM", value=Decimal("10"))
        self.to_confirm_step(discount_code="WELKOM")
        self.confirm(expected_total="180.00")
        order = Order.objects.get()
        self.assertEqual(order.discount_code, "WELKOM")
        self.assertEqual(order.total_price, Decimal("180.00"))
        self.assertEqual(Payment.objects.get().amount, Decimal("180.00"))

    def test_changed_price_starts_no_payment(self):
        self.to_confirm_step()
        response = self.confirm(expected_total="150.00")
        self.assertContains(response, "De prijs van dit verblijf is gewijzigd.")
        self.assertFalse(Order.objects.exists())
        self.assertFalse(Payment.objects.exists())


class DoubleSubmitOnlineTests(CheckoutPaymentTestCase):
    """A confirmation that is sent twice books once and starts one payment."""

    def session_before_confirm(self):
        self.to_confirm_step()
        return dict(self.client.session.items())

    def restore_session(self, saved):
        session = self.client.session
        session.update(saved)
        session.save()

    def test_second_confirmation_goes_to_the_same_payment_page(self):
        saved = self.session_before_confirm()
        first = self.confirm()
        payment = Payment.objects.get()

        self.restore_session(saved)
        with mock.patch(
            "maykinbooking.base.views.start_online_payment"
        ) as start_online_payment:
            second = self.confirm()

        start_online_payment.assert_not_called()
        self.assertEqual(second["Location"], first["Location"])
        self.assertEqual(second["Location"], payment.checkout_url)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(FakeProviderPayment.objects.count(), 1)

    def test_second_confirmation_before_the_payment_exists_goes_to_my_booking(self):
        saved = self.session_before_confirm()
        token = uuid.UUID(self.session_token())
        # The first request has made the order, but not its payment yet.
        order = OrderFactory(
            hotel=self.hotel,
            room=self.room,
            token=token,
            status=OrderStatus.PENDING_PAYMENT,
            payment_status=PaymentStatus.OPEN,
        )
        self.restore_session(saved)

        response = self.confirm()

        self.assertRedirects(
            response,
            reverse("bookings:detail", args=[order.token]),
            fetch_redirect_response=False,
        )
        self.assertFalse(Payment.objects.exists())
        response = self.client.get(response["Location"])
        self.assertContains(response, "Je boeking wacht op je betaling.")
        self.assertContains(response, "We wachten op je betaling.")

    def test_request_that_did_not_make_the_order_starts_no_payment(self):
        """
        Two requests pass the first check; the other one books while this one
        waits for the room. This one finds the order after the lock (in
        ``book_room``, created=False) and starts no payment.
        """
        self.session_before_confirm()
        token = uuid.UUID(self.session_token())

        def other_request_books(email):
            OrderFactory(
                hotel=self.hotel,
                room=self.room,
                token=token,
                status=OrderStatus.PENDING_PAYMENT,
                payment_status=PaymentStatus.OPEN,
            )

        with mock.patch(
            "maykinbooking.base.views.Checkout.check_throttle",
            side_effect=other_request_books,
        ), mock.patch(
            "maykinbooking.base.views.start_online_payment"
        ) as start_online_payment:
            response = self.confirm()

        start_online_payment.assert_not_called()
        order = Order.objects.get()
        self.assertEqual(order.token, token)
        self.assertRedirects(
            response,
            reverse("bookings:detail", args=[order.token]),
            fetch_redirect_response=False,
        )

    def test_expired_payment_page_is_not_offered_again(self):
        saved = self.session_before_confirm()
        self.confirm()
        Payment.objects.update(status=PaymentState.CANCELED)
        Order.objects.update(status=OrderStatus.EXPIRED)

        self.restore_session(saved)
        response = self.confirm()

        order = Order.objects.get()
        self.assertRedirects(
            response,
            reverse("bookings:detail", args=[order.token]),
            fetch_redirect_response=False,
        )
        self.assertEqual(Payment.objects.count(), 1)


class PaymentStartFailedTests(CheckoutPaymentTestCase):
    def fail_start(self):
        return mock.patch.object(
            FakeProvider,
            "create_payment",
            side_effect=ProviderError("Provider down", retryable=True),
        )

    def test_guest_stays_on_step_three_and_the_room_is_free(self):
        self.to_confirm_step()
        old_token = self.session_token()

        with self.fail_start(), self.assertLogs(
            "maykinbooking.base.views", "INFO"
        ) as logs:
            response = self.confirm()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["wizard"]["steps"].current, "confirm")
        self.assertContains(response, START_FAILED)
        self.assertContains(response, 'role="alert"')
        self.assertIn(
            "INFO:maykinbooking.base.views:checkout online payment could not be "
            "started",
            logs.output,
        )
        # The order expired: the room is free and nothing is mailed.
        order = Order.objects.get()
        self.assertEqual(order.status, OrderStatus.EXPIRED)
        self.assertEqual(order.token.hex, old_token)
        self.assertEqual(Payment.objects.get().status, PaymentState.ERROR)
        self.assertEqual(mail.outbox, [])
        # The entered data is kept, the choice too.
        self.assertEqual(response.context["form"]["payment_method"].value(), "online")
        self.assertContains(response, "alex@example.com")

        # A new token: confirming again books anew (here at the hotel).
        new_token = self.session_token()
        self.assertNotEqual(new_token, old_token)
        response = self.confirm(payment_method="at_hotel")
        new_order = Order.objects.get(token=uuid.UUID(new_token))
        self.assertRedirects(
            response,
            reverse("bookings:detail", args=[new_order.token]),
            fetch_redirect_response=False,
        )
        self.assertEqual(new_order.status, OrderStatus.CONFIRMED)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.EXPIRED)

    def test_trying_online_again_starts_a_new_payment(self):
        self.to_confirm_step()
        with self.fail_start():
            self.confirm()
        response = self.confirm()
        new_payment = Payment.objects.get(status=PaymentState.OPEN)
        self.assertEqual(response["Location"], new_payment.checkout_url)
        self.assertEqual(Order.objects.count(), 2)
        self.assertEqual(
            Order.objects.filter(status=OrderStatus.PENDING_PAYMENT).get(),
            new_payment.order,
        )


class FailedPaymentMessageTests(CheckoutPaymentTestCase):
    """The return URL sends the guest back to the checkout with a message."""

    def test_message_is_shown_once_on_the_checkout(self):
        self.to_confirm_step()
        payment_page = path_of(self.confirm()["Location"])
        response = self.client.post(payment_page, {"action": "failed"})
        response = self.client.get(path_of(response["Location"]))
        order = Order.objects.get()
        retry_url = (
            f"{self.url}?van={order.start_date.isoformat()}"
            f"&tot={order.end_date.isoformat()}"
        )
        self.assertRedirects(response, retry_url, fetch_redirect_response=False)

        # The dates are filled in for the next try.
        response = self.client.get(retry_url)
        self.assertContains(response, f'value="{order.start_date.isoformat()}"')
        self.assertContains(response, "De betaling is niet gelukt")
        self.assertContains(response, 'role="alert"')
        self.assertNotContains(self.client.get(self.url), "De betaling is niet gelukt")
