"""
"Mijn boeking" once the stay is over: a confirmed booking is labelled
"Verblijf voorbij" from the day after the departure day on, and the guest no
longer sees a way to cancel it.
"""
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from maykinbooking.base.models import OrderStatus
from maykinbooking.base.tests.factories import HotelFactory, OrderFactory, RoomFactory


class StayOverTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        hotel = HotelFactory(is_available=True, name="Hotel aan de Gracht")
        cls.room = RoomFactory(hotel=hotel)

    def order(self, start_in, nights=2, **kwargs):
        start = timezone.localdate() + timedelta(days=start_in)
        return OrderFactory(
            hotel=self.room.hotel,
            room=self.room,
            start_date=start,
            end_date=start + timedelta(days=nights),
            **kwargs,
        )

    def detail(self, order):
        return self.client.get(
            reverse("bookings:detail", kwargs={"token": order.token})
        )

    def cancel_url(self, order):
        return reverse("bookings:cancel", kwargs={"token": order.token})


class MyBookingTests(StayOverTestCase):
    def test_stay_that_is_over_has_the_label_and_no_cancelling(self):
        for start_in in (-4, -3):  # left two days ago, left yesterday
            with self.subTest(start_in=start_in):
                order = self.order(start_in)

                response = self.detail(order)

                self.assertContains(response, "Verblijf voorbij")
                self.assertNotContains(response, "Bevestigd")
                self.assertNotContains(response, 'id="cancel-title"')
                self.assertNotContains(response, self.cancel_url(order))
                self.assertNotContains(response, "Kosteloos annuleren")
                self.assertNotContains(response, "annuleren.")
                self.assertNotContains(
                    response, "Je betaalt in het hotel bij aankomst."
                )

    def test_current_and_future_stays_are_not_over(self):
        # Leaves today, leaves tomorrow, arrives next week.
        for start_in in (-2, -1, 7):
            with self.subTest(start_in=start_in):
                order = self.order(start_in)

                response = self.detail(order)

                self.assertNotContains(response, "Verblijf voorbij")
                self.assertContains(response, "Bevestigd")
                self.assertContains(response, 'id="cancel-title"')

    def test_cancelled_booking_in_the_past_stays_cancelled(self):
        order = self.order(-5, status=OrderStatus.CANCELLED)

        response = self.detail(order)

        self.assertContains(response, "Geannuleerd")
        self.assertNotContains(response, "Verblijf voorbij")

    def test_status_badges_use_the_shared_colours(self):
        order = self.order(7, status=OrderStatus.PENDING_PAYMENT)

        response = self.detail(order)

        self.assertContains(response, "bg-[#F8EDD6]")
        self.assertNotContains(response, "#FBEFD6")
        self.assertContains(response, "Wacht op betaling")
        self.assertContains(response, "Betalen in het hotel")


class CancelPageTests(StayOverTestCase):
    def test_cancel_page_explains_that_the_stay_is_over(self):
        order = self.order(-3)

        response = self.client.get(self.cancel_url(order))

        self.assertContains(response, "Je verblijf is voorbij")
        self.assertNotContains(response, "Ja, annuleer mijn boeking")
        self.assertNotContains(response, "Wil je toch annuleren?")

    def test_posting_does_not_cancel(self):
        order = self.order(-3)

        response = self.client.post(self.cancel_url(order))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Je verblijf is voorbij")
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CONFIRMED)


class NoReferrerTests(StayOverTestCase):
    """The token in the URL must not leak to other sites through the Referer."""

    def test_pages_ask_the_browser_not_to_send_a_referrer(self):
        order = self.order(7)
        for url in (
            reverse("bookings:detail", kwargs={"token": order.token}),
            self.cancel_url(order),
        ):
            with self.subTest(url=url):
                self.assertContains(
                    self.client.get(url),
                    '<meta name="referrer" content="no-referrer" />',
                    html=False,
                )
