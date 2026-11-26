"""The checkout wizard (US1.2, testpunt 5; phase 2: prices and discount codes)."""
import re
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import Client, TestCase, override_settings
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
from maykinbooking.billing.models import DiscountKind, PricingSettings
from maykinbooking.billing.services.pricing import (
    InvalidDiscountCode,
    check_discount_code,
)
from maykinbooking.billing.tests.factories import (
    DiscountCodeFactory,
    SeasonPeriodFactory,
)
from maykinbooking.billing.tests.utils import TempMediaMixin
from maykinbooking.bookings.services.booking import RoomUnavailable

from .factories import OrderFactory, RoomBlockFactory

INVALID_CODE = "Deze kortingscode is niet geldig."
PRICE_CHANGED = "De prijs van dit verblijf is gewijzigd."
EXPECTED_TOTAL = re.compile(r'name="confirm-expected_total" value="([^"]*)"')

GUEST = {
    "first_name": "Alex",
    "last_name": "de Landgraaf",
    "email": "alex@example.com",
    "address": "Keizersgracht 117",
    "zipcode": "1015 CJ",
    "city": "Amsterdam",
    "country": "NL",
}


class CheckoutTestCase(TempMediaMixin, TestCase):
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

    def confirm(self, **data):
        return self.post_step("confirm", {"terms": "on", **data})

    def expected_total(self, response):
        """The total in the hidden field of step 3, as the browser posts it."""
        match = EXPECTED_TOTAL.search(response.content.decode())
        self.assertIsNotNone(match, "no expected_total on the page")
        return match.group(1)

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
        self.assertContains(response, "1015CJ Amsterdam")
        self.assertEqual(Order.objects.count(), 0)

        response = self.confirm(expected_total=self.expected_total(response))
        order = Order.objects.get()
        detail_url = reverse("bookings:detail", kwargs={"token": order.token})
        self.assertRedirects(response, detail_url, fetch_redirect_response=False)
        self.assertEqual(order.hotel, self.hotel)
        self.assertEqual(order.room, self.room)
        self.assertEqual(order.start_date, self.start)
        self.assertEqual(order.end_date, self.end)
        self.assertEqual(order.zipcode, "1015CJ")
        self.assertEqual(order.city, "Amsterdam")
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
        "maykinbooking.base.views.book_room",
        side_effect=RoomUnavailable("Deze kamer is niet meer vrij in deze periode."),
    )
    def test_room_unavailable_while_booking_shows_step_one(self, book_room):
        # Another guest takes the last room after the revalidation of the steps,
        # but before ``create_booking`` locks the room.
        self.post_stay()
        self.post_guest()

        response = self.confirm()

        book_room.assert_called_once()
        self.assert_on_stay_step(response)
        self.assertContains(response, "Het boeken is niet gelukt.")
        self.assertContains(response, "Deze kamer is niet meer vrij in deze periode.")
        self.assert_dates_kept(response)
        self.assertEqual(Order.objects.count(), 0)

        # The wizard data is intact: the guest details are still there.
        response = self.post_stay()
        self.assertContains(response, 'value="alex@example.com"')

    @mock.patch(
        "maykinbooking.base.views.book_room",
        side_effect=ValidationError("Je kunt niet inchecken in het verleden."),
    )
    def test_invalid_period_while_booking_shows_step_one(self, book_room):
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

    def test_second_confirmation_for_the_last_room_goes_to_the_booking(self):
        # The first confirmation took the last room: the steps of the second
        # one are no longer valid, but it goes to the booking all the same.
        Room.objects.filter(pk=self.room.pk).update(inventory=1)
        saved = self.session_before_confirm()
        first = self.confirm()

        self.restore_session(saved)
        second = self.confirm()

        self.assertEqual(second["Location"], first["Location"])
        self.assertEqual(Order.objects.count(), 1)
        # The stale checkout is cleared: a new one gets a new token.
        self.assertNotIn(
            "booking_token", self.client.session["wizard_checkout"]["extra_data"]
        )

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
            # The other request (for another room) inserted the token first;
            # book_room finds its order after the IntegrityError.
            OrderFactory(hotel=self.hotel, room=self.room, token=kwargs["token"])
            raise IntegrityError("UNIQUE constraint failed: base_order.token")

        with mock.patch(
            "maykinbooking.bookings.services.booking._book_room",
            side_effect=other_request_wins,
        ), mock.patch("maykinbooking.base.views.start_online_payment") as start:
            response = self.confirm()

        start.assert_not_called()

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


class CheckoutPriceTests(CheckoutTestCase):
    """Phase 2: the price of every night, the discounts and the total."""

    def setUp(self):
        super().setUp()
        # From a Thursday to the Sunday: two weekend nights.
        start = timezone.localdate() + timedelta(days=7)
        self.start = start + timedelta(days=(3 - start.weekday()) % 7)
        self.end = self.start + timedelta(days=3)
        self.rules = PricingSettings.load()

    def set_rules(self, **values):
        PricingSettings.objects.filter(pk=1).update(**values)

    def test_without_price_rules_every_night_costs_the_room_price(self):
        response = self.post_stay()
        self.assertContains(response, "€ 100 × 3 nachten")
        self.assertNotContains(response, "weekend +")
        self.assertEqual(response.context["stay"]["total"], Decimal("300.00"))

    def test_weekend_surcharge_per_night(self):
        self.set_rules(weekend_percent=Decimal("20"))
        response = self.post_stay()
        stay = response.context["stay"]
        self.assertIsNone(stay["uniform_price"])
        self.assertEqual(
            [(line["price"], line["surcharges"]) for line in stay["night_lines"]],
            [
                (Decimal("100.00"), []),
                (Decimal("120.00"), ["weekend +20%"]),
                (Decimal("120.00"), ["weekend +20%"]),
            ],
        )
        # The summary: the nights, then the total.
        self.assertContains(response, "3 nachten")
        self.assertContains(response, "weekend +20%", count=2)
        self.assertContains(response, "€ 340,00", count=2)  # subtotal and total

        # Step 3 shows the same, with the base price.
        response = self.post_guest()
        self.assertContains(response, "Prijsopbouw")
        # In the table of step 3 and in the summary.
        self.assertContains(response, "weekend +20%", count=4)
        self.assertContains(response, "(basis € 100,00)", count=2)
        self.assertEqual(self.expected_total(response), "340.00")
        self.confirm(expected_total="340.00")
        order = Order.objects.get()
        self.assertEqual(order.total_price, Decimal("340.00"))
        self.assertEqual(
            list(order.nights.values_list("price", "weekend_percent")),
            [
                (Decimal("100.00"), Decimal("0.00")),
                (Decimal("120.00"), Decimal("20.00")),
                (Decimal("120.00"), Decimal("20.00")),
            ],
        )

    def test_season_surcharge_with_its_name(self):
        SeasonPeriodFactory(
            name="Koningsdag", start_date=self.start, end_date=self.start, percent=50
        )
        response = self.post_stay()
        self.assertContains(response, "Koningsdag +50%")
        self.assertEqual(response.context["stay"]["total"], Decimal("350.00"))

    def test_long_stay_discount(self):
        self.set_rules(long_stay_min_nights=3, long_stay_percent=Decimal("10"))
        response = self.post_stay()
        self.assertContains(response, "Korting lang verblijf (10%)")
        self.assertContains(response, "− € 30,00")
        self.assertContains(response, "€ 270,00")
        self.assertEqual(response.context["stay"]["subtotal"], Decimal("300.00"))

    def test_many_nights_are_folded_away_in_the_summary(self):
        self.end = self.start + timedelta(days=8)
        response = self.post_stay()
        self.assertFalse(response.context["stay"]["nights_open"])
        self.assertContains(response, '<details class="group">')
        self.end = self.start + timedelta(days=7)
        response = self.post_stay()
        self.assertContains(response, '<details class="group" open>')


class CheckoutDiscountCodeTests(CheckoutTestCase):
    """Phase 2: the optional discount code of step 1."""

    def setUp(self):
        super().setUp()
        self.code = DiscountCodeFactory(code="WELKOM", value=Decimal("10"))

    def test_code_field_is_folded_away_until_used(self):
        unfolded = 'border-seam p-5" open>'
        response = self.client.get(self.url)
        self.assertContains(response, "Heb je een kortingscode?")
        self.assertContains(response, 'name="stay-discount_code"')
        self.assertNotContains(response, unfolded)
        self.assertContains(self.post_stay(discount_code="NEP"), unfolded)

    def test_valid_code_gives_its_discount(self):
        response = self.post_stay(discount_code="welkom")
        self.assertContains(response, "Wie verblijft er?")
        self.assertContains(response, "Kortingscode WELKOM")
        self.assertContains(response, "− € 30,00")
        self.assertContains(response, "€ 270,00")

        response = self.post_guest()
        self.assertContains(response, "Kortingscode WELKOM")
        self.assertEqual(self.expected_total(response), "270.00")
        self.confirm(expected_total="270.00")

        order = Order.objects.get()
        self.assertEqual(order.discount_code, "WELKOM")
        self.assertEqual(order.code_discount, Decimal("30.00"))
        self.assertEqual(order.total_price, Decimal("270.00"))
        self.code.refresh_from_db()
        self.assertEqual(self.code.times_used, 1)
        self.assertEqual(order.discount_redemption.code, self.code)

    def test_invalid_code_stays_on_step_one_with_one_message(self):
        for code in ["NEPCODE", "x", "WELKOM!"]:
            with self.subTest(code=code):
                response = self.post_stay(discount_code=code)
                self.assert_on_stay_step(response)
                self.assertContains(response, INVALID_CODE, count=1)
                # Unfolded, and the field points at its error.
                self.assertContains(
                    response,
                    'aria-invalid="true" aria-describedby="id_stay-discount_code_errors"',
                )
                self.assertContains(response, 'id="id_stay-discount_code_errors"')
        self.assertEqual(Order.objects.count(), 0)

    def test_used_up_code_gets_the_same_message(self):
        DiscountCodeFactory(code="OPGEBRUIKT", max_uses=1, times_used=1)
        response = self.post_stay(discount_code="OPGEBRUIKT")
        self.assertContains(response, INVALID_CODE, count=1)

    def test_code_used_up_before_confirming_sends_the_guest_back(self):
        self.code.max_uses = 1
        self.code.save()
        self.post_stay(discount_code="WELKOM")
        self.post_guest()
        self.code.times_used = 1  # the last use went to another guest
        self.code.save()

        response = self.confirm()

        self.assert_on_stay_step(response)
        self.assertContains(response, INVALID_CODE, count=1)
        self.assertEqual(Order.objects.count(), 0)
        # Without the code the guest can go on; the details are kept.
        response = self.post_stay(discount_code="")
        self.assertContains(response, 'value="alex@example.com"')

    def test_code_used_up_while_booking_sends_the_guest_back(self):
        self.post_stay(discount_code="WELKOM")
        self.post_guest()
        with mock.patch(
            "maykinbooking.base.views.book_room",
            side_effect=InvalidDiscountCode("used_up"),
        ):
            with self.assertLogs("maykinbooking.base.views", "INFO") as logs:
                response = self.confirm()

        self.assert_on_stay_step(response)
        self.assertContains(response, INVALID_CODE, count=1)
        self.assertContains(response, 'value="WELKOM"')
        self.assertEqual(Order.objects.count(), 0)
        self.assertIn("checkout discount code refused reason=used_up", logs.output[0])

    def test_fixed_code_never_takes_more_than_the_price(self):
        DiscountCodeFactory(
            code="CADEAU", kind=DiscountKind.FIXED, value=Decimal("500")
        )
        self.post_stay(discount_code="CADEAU")
        response = self.post_guest()
        self.assertContains(response, "− € 300,00")
        self.assertEqual(self.expected_total(response), "0.00")
        self.confirm(expected_total="0.00")
        self.assertEqual(Order.objects.get().total_price, Decimal("0.00"))

    @mock.patch("maykinbooking.base.views.DISCOUNT_CODE_LIMIT_PER_IP_PER_HOUR", 2)
    def test_tries_per_ip_address_are_limited(self):
        for code in ["NEP-1", "NEP-2"]:
            self.assertContains(self.post_stay(discount_code=code), INVALID_CODE)

        with mock.patch(
            "maykinbooking.form.forms.check_discount_code"
        ) as check_discount_code, self.assertLogs(
            "maykinbooking.base.views", "INFO"
        ) as logs:
            response = self.post_stay(discount_code="WELKOM")

        # The valid code is refused too, without looking it up.
        check_discount_code.assert_not_called()
        self.assert_on_stay_step(response)
        self.assertContains(response, "Je hebt te vaak een kortingscode geprobeerd")
        self.assertNotContains(response, INVALID_CODE)
        self.assertEqual(
            logs.output,
            ["INFO:maykinbooking.base.views:discount code attempts throttled"],
        )

        # Going on without a code still works, and so does another address.
        response = self.post_stay(discount_code="")
        self.assertContains(response, "Wie verblijft er?")
        self.client.get(self.url, REMOTE_ADDR="192.0.2.99")
        self.client.defaults["REMOTE_ADDR"] = "192.0.2.99"
        response = self.post_stay(discount_code="WELKOM")
        self.assertContains(response, "Kortingscode WELKOM")

    @mock.patch("maykinbooking.base.views.DISCOUNT_CODE_LIMIT_PER_IP_PER_HOUR", 1)
    def test_only_posting_step_one_counts_as_a_try(self):
        self.post_stay(discount_code="WELKOM")
        # The later steps check the stored code again, without counting.
        self.post_guest()
        response = self.confirm()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Order.objects.get().discount_code, "WELKOM")

    def test_default_limit(self):
        from maykinbooking.base import views

        self.assertEqual(views.DISCOUNT_CODE_LIMIT_PER_IP_PER_HOUR, 10)

    def post_code(self, code, *, client=None, **extra):
        payload = {
            "checkout-current_step": "stay",
            "stay-start_date": self.start,
            "stay-end_date": self.end,
            "stay-room": self.room.pk,
            "stay-discount_code": code,
            **extra,
        }
        return (client or self.client).post(self.url, payload)

    def test_a_later_step_without_a_session_does_not_dodge_the_limit(self):
        """
        Security B1: without a session cookie formtools validates step 1 with
        the posted data, whatever step the post says it is for. The code is
        counted where it is looked up, so that counts too.
        """
        for step in ("guest", "confirm"):
            with self.subTest(step=step):
                cache.clear()
                with mock.patch(
                    "maykinbooking.form.forms.check_discount_code",
                    wraps=check_discount_code,
                ) as lookup:
                    for attempt in range(10):
                        response = self.post_code(
                            f"NEP-{attempt}",
                            client=Client(),
                            **{"checkout-current_step": step},
                        )
                        self.assertContains(response, INVALID_CODE)
                    response = self.post_code(
                        "WELKOM", client=Client(), **{"checkout-current_step": step}
                    )
                self.assertContains(
                    response, "Je hebt te vaak een kortingscode geprobeerd"
                )
                self.assertNotContains(response, "Kortingscode WELKOM")
                # The 11th was refused without looking it up.
                self.assertEqual(lookup.call_count, 10)

    def test_a_step_that_does_not_exist_is_a_bad_request(self):
        for client in (self.client, Client()):
            with self.subTest(session=client is self.client):
                response = self.post_code(
                    "WELKOM", client=client, **{"checkout-current_step": "x"}
                )
                self.assertEqual(response.status_code, 400)
        self.assertEqual(Order.objects.count(), 0)

    @mock.patch("maykinbooking.base.views.DISCOUNT_CODE_LIMIT_PER_IP_PER_HOUR", 1)
    def test_validating_the_stored_code_again_does_not_count(self):
        self.post_code("WELKOM")
        # Steps 2 and 3 validate the stored step 1 again (and look the code up)
        # several times: no tries.
        self.post_guest()
        self.post_step("guest", GUEST)  # step 3 again: one more revalidation
        response = self.confirm(expected_total="270.00")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Order.objects.get().discount_code, "WELKOM")

    def test_a_made_up_step_to_go_to_does_not_dodge_the_limit(self):
        """
        Security B1: ``wizard_goto_step`` with a step that does not exist is not
        "going back": the posted code is checked, so it counts.
        """
        for attempt in range(10):
            response = self.post_code(f"NEP-{attempt}", wizard_goto_step="x")
            self.assertContains(response, INVALID_CODE)
        response = self.post_code("WELKOM", wizard_goto_step="x")
        self.assertContains(response, "Je hebt te vaak een kortingscode geprobeerd")
        self.assertNotContains(response, "Kortingscode WELKOM")

    @mock.patch("maykinbooking.base.views.DISCOUNT_CODE_LIMIT_PER_IP_PER_HOUR", 2)
    def test_going_back_to_a_real_step_does_not_count(self):
        self.post_code("WELKOM")
        self.post_guest()
        # Going back renders a step without checking what was posted.
        for _ in range(3):
            self.post_code("NEP-1", wizard_goto_step="stay")
        # The second real try, so within the limit.
        response = self.post_code("WELKOM")
        self.assertNotContains(response, "Je hebt te vaak een kortingscode geprobeerd")


class CheckoutPriceChangedTests(CheckoutTestCase):
    """The price changes between the review and the confirmation."""

    def test_new_price_is_shown_and_booked_after_confirming_again(self):
        self.post_stay()
        response = self.post_guest()
        self.assertEqual(self.expected_total(response), "300.00")
        Room.objects.filter(pk=self.room.pk).update(price=Decimal("120"))

        with self.assertLogs("maykinbooking.base.views", "INFO") as logs:
            response = self.confirm(expected_total="300.00")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["wizard"]["steps"].current, "confirm")
        self.assertContains(response, PRICE_CHANGED)
        self.assertContains(response, 'role="alert"')
        self.assertEqual(response.context["stay"]["total"], Decimal("360.00"))
        self.assertEqual(self.expected_total(response), "360.00")
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(
            logs.output, ["INFO:maykinbooking.base.views:checkout price changed"]
        )

        response = self.confirm(expected_total=self.expected_total(response))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Order.objects.get().total_price, Decimal("360.00"))

    def test_changed_price_rules(self):
        PricingSettings.load()
        self.post_stay()
        self.post_guest()
        PricingSettings.objects.filter(pk=1).update(
            long_stay_min_nights=2, long_stay_percent=Decimal("50")
        )
        response = self.confirm(expected_total="300.00")
        self.assertContains(response, PRICE_CHANGED)
        self.assertEqual(self.expected_total(response), "150.00")

    def test_a_made_up_total_is_never_the_price(self):
        """Security: the posted total is only compared, never booked."""
        self.post_stay()
        self.post_guest()
        response = self.confirm(expected_total="1.00")
        self.assertContains(response, PRICE_CHANGED)
        self.assertEqual(Order.objects.count(), 0)

    def test_unreadable_total_asks_to_confirm_again(self):
        self.post_stay()
        self.post_guest()
        response = self.confirm(expected_total="drie euro")
        self.assertContains(
            response, "De prijs kon niet gecontroleerd worden. Bevestig opnieuw."
        )
        self.assertEqual(Order.objects.count(), 0)
        response = self.confirm(expected_total=self.expected_total(response))
        self.assertEqual(response.status_code, 302)
