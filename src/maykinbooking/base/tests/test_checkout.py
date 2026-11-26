"""The checkout wizard (US1.2, testpunt 5)."""
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from freezegun import freeze_time

from maykinbooking.base.models import (
    City,
    EmailLog,
    EmailStatus,
    Hotel,
    Order,
    OrderStatus,
    PaymentStatus,
    Room,
)
from maykinbooking.bookings.services.booking import RoomUnavailable

from .factories import OrderFactory, RoomBlockFactory

GUEST = {
    "first_name": "Alex",
    "last_name": "de Landgraaf",
    "email": "alex@example.com",
    "address": "Keizersgracht 117",
    "zipcode": "1015 CJ",
    "country": "NL",
}


class CheckoutTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        city = City.objects.create(city_id="AMS", name="Amsterdam")
        cls.hotel = Hotel.objects.create(
            city=city, hotel_id="AMS01", name="Ibis Amsterdam"
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
        # The rate limits count in the cache, which all tests share.
        cache.clear()
        self.start = timezone.localdate() + timedelta(days=7)
        self.end = self.start + timedelta(days=3)

    def post_step(self, step, data, url=None):
        payload = {"checkout-current_step": step}
        payload.update({f"{step}-{key}": value for key, value in data.items()})
        return self.client.post(url or self.url, payload)

    def post_stay(self, **overrides):
        data = {"start_date": self.start, "end_date": self.end, "room": self.room.pk}
        data.update(overrides)
        return self.post_step("stay", data)

    def post_guest(self, **overrides):
        return self.post_step("guest", {**GUEST, **overrides})

    def confirm(self):
        return self.post_step("confirm", {"terms": "on"})

    def fill_room(self, room=None):
        """Another guest books the last room for the same period."""
        return OrderFactory(
            hotel=self.hotel,
            room=room or self.room,
            start_date=self.start,
            end_date=self.end,
        )

    def assert_on_stay_step(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Kies je kamer")
        self.assertEqual(response.context["wizard"]["steps"].current, "stay")

    def assert_dates_kept(self, response):
        self.assertContains(response, f'value="{self.start.isoformat()}"')
        self.assertContains(response, f'value="{self.end.isoformat()}"')


class CheckoutTests(CheckoutTestCase):
    def test_first_step_lists_rooms(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Kies je kamer")
        self.assertContains(response, "Double")

    def test_unknown_hotel_is_404(self):
        response = self.client.get(reverse("order", args=[999999]))
        self.assertEqual(response.status_code, 404)

    def test_full_checkout_creates_one_order(self):
        self.client.get(self.url)
        response = self.post_stay()
        self.assertContains(response, "Wie verblijft er?")
        # The summary shows the price breakdown after step 1.
        self.assertContains(response, "× 3 nachten")
        self.assertContains(response, "€ 300,00")

        response = self.post_guest()
        self.assertContains(response, "Klopt alles?")
        self.assertContains(response, "alex@example.com")
        self.assertEqual(Order.objects.count(), 0)

        response = self.confirm()
        order = Order.objects.get()
        detail_url = reverse("bookings:detail", kwargs={"token": order.token})
        self.assertRedirects(response, detail_url, fetch_redirect_response=False)
        self.assertEqual(order.hotel, self.hotel)
        self.assertEqual(order.room, self.room)
        self.assertEqual(order.start_date, self.start)
        self.assertEqual(order.end_date, self.end)
        self.assertEqual(order.zipcode, "1015CJ")
        self.assertEqual(order.total_price, Decimal("300"))
        self.assertEqual(order.status, OrderStatus.CONFIRMED)
        self.assertEqual(order.payment_status, PaymentStatus.AT_HOTEL)
        self.assertEqual(order.nights.count(), 3)

        response = self.client.get(detail_url)
        self.assertContains(response, "Je boeking staat vast")
        self.assertContains(response, "€ 300,00")

    def test_order_language_is_the_language_code_not_the_region(self):
        self.post_stay()
        self.post_guest()
        self.confirm()
        self.assertEqual(Order.objects.get().language, "nl")

    def test_booking_is_logged_as_made_by_the_guest(self):
        self.post_stay()
        self.post_guest()
        self.confirm()
        actor_types = set(
            Order.objects.get().status_changes.values_list("actor_type", flat=True)
        )
        self.assertEqual(actor_types, {"guest"})

    def test_success_message_is_shown_once(self):
        self.post_stay()
        self.post_guest()
        detail_url = self.confirm().url
        self.assertContains(self.client.get(detail_url), "Je boeking staat vast")
        self.assertNotContains(self.client.get(detail_url), "Je boeking staat vast")

    def test_terms_must_be_accepted(self):
        self.client.get(self.url)
        self.post_stay()
        self.post_guest()
        response = self.post_step("confirm", {})
        self.assertContains(response, "Je moet akkoord gaan met de voorwaarden")
        self.assertEqual(Order.objects.count(), 0)

    def test_invalid_dates_stay_on_first_step(self):
        self.client.get(self.url)
        response = self.post_stay(end_date=self.start)
        self.assertContains(response, "Uitchecken moet na de incheckdatum zijn.")
        self.assert_on_stay_step(response)

    def test_check_in_in_the_past_stays_on_first_step(self):
        response = self.post_stay(start_date=timezone.localdate() - timedelta(days=1))
        self.assertContains(response, "Je kunt niet inchecken in het verleden.")
        self.assert_on_stay_step(response)

    def test_room_that_became_unavailable_is_caught_on_confirm(self):
        self.client.get(self.url)
        self.post_stay()
        self.post_guest()
        Room.objects.filter(pk=self.room.pk).update(is_available=False)
        response = self.confirm()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Order.objects.count(), 0)

    def test_hotel_without_available_rooms_cannot_be_booked(self):
        Room.objects.filter(pk=self.room.pk).update(is_available=False)
        response = self.client.get(self.url)
        self.assertContains(response, "niet te boeken")
        self.post_stay()
        self.assertEqual(Order.objects.count(), 0)

    def test_hotel_with_only_rooms_without_inventory_cannot_be_booked(self):
        Room.objects.filter(pk=self.room.pk).update(inventory=0)
        response = self.client.get(self.url)
        self.assertContains(response, "niet te boeken")


class CheckoutPeriodFromQuerystringTests(CheckoutTestCase):
    """``?van=&tot=`` from the city or hotel page fills in step 1."""

    def get_with_period(self, start=None, end=None):
        return self.client.get(
            self.url,
            {
                "van": (start or self.start).isoformat(),
                "tot": (end or self.end).isoformat(),
            },
        )

    def test_dates_are_initial(self):
        response = self.get_with_period()
        self.assertEqual(response.status_code, 200)
        self.assert_dates_kept(response)

    def test_without_querystring_the_dates_are_empty(self):
        response = self.client.get(self.url)
        self.assertNotContains(response, f'value="{self.start.isoformat()}"')

    def test_invalid_dates_are_ignored(self):
        for params in [
            {"van": "morgen", "tot": "overmorgen"},
            {"van": "2027-02-30", "tot": "2027-03-02"},
            {"van": self.start.isoformat()},
        ]:
            with self.subTest(params=params):
                response = self.client.get(self.url, params)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Kies je kamer")
                self.assertContains(response, f'value="{self.room.pk}"')

    def test_only_free_rooms_are_listed(self):
        single = Room.objects.create(
            hotel=self.hotel, room_type="Single", price=Decimal("80"), description="x"
        )
        self.fill_room()
        response = self.get_with_period()
        self.assertContains(response, f'value="{single.pk}"')
        self.assertNotContains(response, f'value="{self.room.pk}"')
        # The same room is listed for another period.
        response = self.get_with_period(self.end, self.end + timedelta(days=2))
        self.assertContains(response, f'value="{self.room.pk}"')

    def test_no_free_room_is_announced(self):
        self.fill_room()
        response = self.get_with_period()
        self.assertContains(response, "Geen kamers vrij in deze periode.")
        self.assertNotContains(response, f'value="{self.room.pk}"')

    def test_blocked_room_is_not_listed(self):
        RoomBlockFactory(room=self.room, start_date=self.start, end_date=self.end)
        response = self.get_with_period()
        self.assertContains(response, "Geen kamers vrij in deze periode.")

    def test_full_period_cannot_be_posted(self):
        self.fill_room()
        response = self.post_stay()
        self.assert_on_stay_step(response)
        self.assertEqual(
            response.context["form"].non_field_errors(),
            ["Geen kamers vrij in deze periode."],
        )
        self.assertEqual(Order.objects.count(), 1)


class CheckoutRoomFullBeforeConfirmTests(CheckoutTestCase):
    """Testpunt 5: the room fills up between step 1 and confirming."""

    def test_full_room_sends_guest_back_to_step_one(self):
        self.post_stay()
        self.post_guest()
        other = self.fill_room()

        response = self.confirm()

        self.assert_on_stay_step(response)
        self.assertContains(response, "Geen kamers vrij in deze periode.")
        self.assertEqual(list(Order.objects.all()), [other])
        # The dates are still filled in ...
        self.assert_dates_kept(response)

        # ... and so are the guest details, after choosing another period.
        new_start = self.end
        response = self.post_stay(
            start_date=new_start, end_date=new_start + timedelta(days=2)
        )
        self.assertContains(response, "Wie verblijft er?")
        self.assertContains(response, 'value="alex@example.com"')
        self.post_guest()
        response = self.confirm()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Order.objects.count(), 2)

    def test_full_room_with_other_free_room_shows_a_room_error(self):
        single = Room.objects.create(
            hotel=self.hotel, room_type="Single", price=Decimal("80"), description="x"
        )
        self.post_stay()
        self.post_guest()
        self.fill_room()

        response = self.confirm()

        self.assert_on_stay_step(response)
        self.assertContains(response, "Deze kamer is niet vrij in deze periode.")
        self.assertContains(response, f'value="{single.pk}"')
        self.assertEqual(Order.objects.count(), 1)

    @mock.patch(
        "maykinbooking.base.views.create_booking",
        side_effect=RoomUnavailable("Deze kamer is niet meer vrij in deze periode."),
    )
    def test_room_unavailable_while_booking_shows_step_one(self, create_booking):
        # Another guest takes the last room after the revalidation of the steps,
        # but before ``create_booking`` locks the room.
        self.post_stay()
        self.post_guest()

        response = self.confirm()

        create_booking.assert_called_once()
        self.assert_on_stay_step(response)
        self.assertContains(response, "Het boeken is niet gelukt.")
        self.assertContains(response, "Deze kamer is niet meer vrij in deze periode.")
        self.assert_dates_kept(response)
        self.assertEqual(Order.objects.count(), 0)

        # The wizard data is intact: the guest details are still there.
        response = self.post_stay()
        self.assertContains(response, 'value="alex@example.com"')

    @mock.patch(
        "maykinbooking.base.views.create_booking",
        side_effect=ValidationError("Je kunt niet inchecken in het verleden."),
    )
    def test_invalid_period_while_booking_shows_step_one(self, create_booking):
        self.post_stay()
        self.post_guest()

        response = self.confirm()

        self.assert_on_stay_step(response)
        self.assertContains(response, "Het boeken is niet gelukt.")
        self.assertContains(response, "Je kunt niet inchecken in het verleden.")
        self.assertEqual(Order.objects.count(), 0)


class CheckoutMailTests(CheckoutTestCase):
    def test_booking_sends_one_mail_with_a_working_link(self):
        self.post_stay()
        self.post_guest()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.confirm()

        order = Order.objects.get()
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["alex@example.com"])
        path = reverse("bookings:detail", kwargs={"token": order.token})
        self.assertIn(settings.SITE_URL.rstrip("/") + path, message.body)
        self.assertEqual(EmailLog.objects.get(order=order).status, EmailStatus.SENT)
        self.assertRedirects(response, path)
        self.assertEqual(self.client.get(path).status_code, 200)

    def test_no_mail_when_the_booking_fails(self):
        self.post_stay()
        self.post_guest()
        self.fill_room()
        with self.captureOnCommitCallbacks(execute=True):
            self.confirm()
        self.assertEqual(len(mail.outbox), 0)


class OldGuestUrlsTests(CheckoutTestCase):
    """The session based success and PDF pages are replaced by "Mijn boeking"."""

    def test_old_urls_are_gone(self):
        order = OrderFactory(hotel=self.hotel, room=self.room)
        for path in [f"/success/{order.pk}", f"/orderpdf/{order.pk}"]:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)


class DoubleSubmitTests(CheckoutTestCase):
    """
    Review should-fix 2: confirming twice books once. The second request is
    simulated by putting back the session as it was before the first one, like
    two requests that read the session at the same time.
    """

    def setUp(self):
        super().setUp()
        Room.objects.filter(pk=self.room.pk).update(inventory=5)

    def session_before_confirm(self):
        self.post_stay()
        self.post_guest()
        return dict(self.client.session.items())

    def restore_session(self, saved):
        session = self.client.session
        session.update(saved)
        session.save()

    def test_second_confirmation_goes_to_the_same_booking_without_a_second_mail(
        self,
    ):
        saved = self.session_before_confirm()
        with self.captureOnCommitCallbacks(execute=True):
            first = self.confirm()
        order = Order.objects.get()
        detail_url = reverse("bookings:detail", kwargs={"token": order.token})
        self.assertRedirects(first, detail_url, fetch_redirect_response=False)

        self.restore_session(saved)
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            second = self.confirm()

        self.assertRedirects(second, detail_url, fetch_redirect_response=False)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(callbacks, [])
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(order.status_changes.count(), 2)

    def test_token_is_made_at_the_first_step_and_kept(self):
        self.post_stay()
        token = self.client.session["wizard_checkout"]["extra_data"]["booking_token"]
        self.post_guest()
        self.post_stay()  # back to step 1 and on again: the same checkout
        session = self.client.session["wizard_checkout"]
        self.assertEqual(session["extra_data"]["booking_token"], token)

        self.confirm()
        self.assertEqual(Order.objects.get().token.hex, token)

    def test_a_new_checkout_gets_a_new_token(self):
        self.post_stay()
        self.post_guest()
        self.confirm()
        self.client.get(self.url)  # starts a new checkout
        self.post_stay()
        self.post_guest()
        self.confirm()
        self.assertEqual(Order.objects.count(), 2)
        self.assertEqual(Order.objects.values("token").distinct().count(), 2)

    def test_lost_race_on_the_unique_token_goes_to_the_winning_booking(self):
        self.session_before_confirm()
        token = uuid.UUID(
            self.client.session["wizard_checkout"]["extra_data"]["booking_token"]
        )

        def other_request_wins(**kwargs):
            OrderFactory(hotel=self.hotel, room=self.room, token=kwargs["token"])
            raise IntegrityError("UNIQUE constraint failed: base_order.token")

        with mock.patch(
            "maykinbooking.base.views.create_booking", side_effect=other_request_wins
        ):
            response = self.confirm()

        detail_url = reverse("bookings:detail", kwargs={"token": token})
        self.assertRedirects(response, detail_url, fetch_redirect_response=False)
        self.assertEqual(Order.objects.get().token, token)


@override_settings(BOOKING_LIMIT_PER_IP_PER_HOUR=2, BOOKING_LIMIT_PER_EMAIL_PER_DAY=2)
class CheckoutThrottleTests(CheckoutTestCase):
    """Security S1: a limit on bookings per IP address and per e-mail address."""

    def setUp(self):
        super().setUp()
        Room.objects.filter(pk=self.room.pk).update(inventory=10)

    def book(self, email="alex@example.com", ip="192.0.2.1"):
        self.client.get(self.url, REMOTE_ADDR=ip)
        self.client.defaults["REMOTE_ADDR"] = ip
        self.post_stay()
        self.post_guest(email=email)
        return self.confirm()

    def assert_throttled(self, response):
        self.assert_on_stay_step(response)
        self.assertContains(response, "al veel boekingen gemaakt")
        self.assertContains(response, "Probeer het later opnieuw")

    def test_limit_per_ip_address(self):
        for number in range(2):
            response = self.book(email=f"gast{number}@example.com")
            self.assertEqual(response.status_code, 302)

        response = self.book(email="gast3@example.com")

        self.assert_throttled(response)
        self.assertEqual(Order.objects.count(), 2)
        # Another IP address can still book.
        self.assertEqual(
            self.book(email="gast4@example.com", ip="192.0.2.2").status_code, 302
        )

    def test_limit_per_email_address_ignores_case_and_spaces(self):
        self.book(email="alex@example.com", ip="192.0.2.1")
        self.book(email="Alex@Example.com", ip="192.0.2.2")

        response = self.book(email="  ALEX@example.com ", ip="192.0.2.3")

        self.assert_throttled(response)
        self.assertEqual(Order.objects.count(), 2)

    def test_plus_suffixes_count_as_the_same_address(self):
        self.book(email="alex+1@example.com", ip="192.0.2.1")
        self.book(email="Alex+2@example.com", ip="192.0.2.2")

        response = self.book(email="alex+3@example.com", ip="192.0.2.3")

        self.assert_throttled(response)
        # Only the counting ignores the suffix: the bookings keep the address.
        self.assertEqual(
            sorted(Order.objects.values_list("email", flat=True)),
            ["Alex+2@example.com", "alex+1@example.com"],
        )

    def test_throttled_guest_keeps_the_entered_data(self):
        self.book(email="a@example.com")
        self.book(email="b@example.com")
        response = self.book(email="c@example.com")
        self.assert_throttled(response)
        self.assert_dates_kept(response)
        response = self.post_stay()
        self.assertContains(response, 'value="c@example.com"')

    def test_limit_per_hour_and_per_day(self):
        with freeze_time("2027-03-01T09:00:00Z") as frozen:
            self.start = timezone.localdate() + timedelta(days=7)
            self.end = self.start + timedelta(days=2)
            self.book(email="a@example.com")
            self.book(email="b@example.com")
            self.assert_throttled(self.book(email="c@example.com"))

            frozen.tick(timedelta(hours=1, seconds=1))
            self.assertEqual(self.book(email="d@example.com").status_code, 302)

            # The e-mail limit is per day.
            self.book(email="d@example.com", ip="192.0.2.9")
            self.assert_throttled(self.book(email="d@example.com", ip="192.0.2.10"))
            frozen.tick(timedelta(days=1))
            response = self.book(email="d@example.com", ip="192.0.2.11")
            self.assertEqual(response.status_code, 302)

    def test_throttling_is_logged_with_the_reason_only(self):
        self.book(email="a@example.com")
        self.book(email="b@example.com")
        with self.assertLogs("maykinbooking.base.views", "INFO") as logs:
            self.assert_throttled(self.book(email="c@example.com"))
        self.assertEqual(
            logs.output, ["INFO:maykinbooking.base.views:checkout throttled reason=ip"]
        )

        # a@example.com books a second time (its limit), elsewhere.
        self.assertEqual(
            self.book(email="a@example.com", ip="192.0.2.77").status_code, 302
        )
        with self.assertLogs("maykinbooking.base.views", "INFO") as logs:
            self.assert_throttled(self.book(email="a@example.com", ip="192.0.2.78"))
            self.assert_throttled(self.book(email="a@example.com"))
        self.assertEqual(
            [line.rsplit(":", 1)[-1] for line in logs.output],
            ["checkout throttled reason=email", "checkout throttled reason=ip,email"],
        )
        for line in logs.output:
            self.assertNotIn("example.com", line)
            self.assertNotIn("192.0.2", line)

    def test_without_client_ip_only_the_email_limit_applies(self):
        # Three different guests without REMOTE_ADDR: they must not share one
        # IP counter (limit 2), so all three can book.
        with self.assertLogs("maykinbooking.bookings.throttle", "WARNING") as logs:
            for number in range(3):
                response = self.book(email=f"gast{number}@example.com", ip="")
                self.assertEqual(response.status_code, 302)
        self.assertIn("booking without client IP address", logs.output[0])
        self.assertEqual(Order.objects.count(), 3)

        # The e-mail limit still applies.
        self.book(email="gast0@example.com", ip="")
        self.assert_throttled(self.book(email="gast0@example.com", ip=""))

    def test_default_limits(self):
        self.assertEqual(settings.BOOKING_LIMIT_PER_IP_PER_HOUR, 2)  # overridden
        from maykinbooking.conf import base

        self.assertEqual(base.BOOKING_LIMIT_PER_IP_PER_HOUR, 5)
        self.assertEqual(base.BOOKING_LIMIT_PER_EMAIL_PER_DAY, 3)
        self.assertEqual(base.BOOKING_PDF_LIMIT_PER_IP_PER_MINUTE, 20)
