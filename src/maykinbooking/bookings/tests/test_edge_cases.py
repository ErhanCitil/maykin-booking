"""
Edge cases of availability, booking and "Mijn boeking", through the pages:

* inventory 0;
* a hotel or room that is set to unavailable while it has a booking;
* 30 versus 31 nights;
* checking in today, where "today" is the date in Amsterdam, not in UTC;
* a room block and bookings on the same nights;
* a booking that waits for payment (hold of 15 minutes);
* a room type whose 5 rooms are all booked.

Time is frozen on Monday 1 March 2027, 10:00 in Amsterdam, unless a test says
otherwise.
"""
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from freezegun import freeze_time

from maykinbooking.accounts.tests.factories import StaffUserFactory
from maykinbooking.base.models import (
    City,
    Hotel,
    Order,
    OrderStatus,
    PaymentStatus,
    Room,
    RoomBlock,
)
from maykinbooking.base.tests.factories import OrderFactory

from ..services.booking import Actor, RoomUnavailable, cancel_booking, create_booking

NOW = "2027-03-01T09:00:00Z"
TODAY = date(2027, 3, 1)

GUEST = {
    "first_name": "Alex",
    "last_name": "de Landgraaf",
    "email": "alex@example.com",
    "address": "Keizersgracht 117",
    "zipcode": "1015CJ",
    "country": "NL",
}


def day(number):
    """``day(10)`` is 10 March 2027."""
    return date(2027, 3, number)


def period(start, end):
    return {"van": start.isoformat(), "tot": end.isoformat()}


@freeze_time(NOW)
class PageTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.city = City.objects.create(city_id="DEL", name="Delft")
        cls.hotel = Hotel.objects.create(
            city=cls.city, hotel_id="DEL01", name="Hotel Oude Delft", is_available=True
        )
        cls.room = Room.objects.create(
            hotel=cls.hotel,
            room_type="Double",
            price=Decimal("100"),
            description="Kamer",
            inventory=1,
        )
        cls.staff = StaffUserFactory(username="medewerker")
        cls.city_url = reverse("stad", args=[cls.city.name])
        cls.order_url = reverse("order", args=[cls.hotel.pk])
        cls.hotel_url = reverse("hotel_detail", args=[cls.hotel.pk])

    def setUp(self):
        # The rate limits count in the cache, which all tests share.
        cache.clear()

    def listed(self, start, end):
        """The names of the hotels on the city page for ``[start, end)``."""
        response = self.client.get(self.city_url, period(start, end))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'role="alert"')
        return [hotel.name for hotel in response.context["object_list"]]

    def offered_rooms(self, start, end):
        """The rooms that step 1 of the checkout offers for ``[start, end)``."""
        response = self.client.get(self.order_url, period(start, end))
        self.assertEqual(response.status_code, 200)
        return list(response.context["form"].fields["room"].queryset)

    def book(self, start, end, room=None):
        return create_booking(
            room_id=(room or self.room).pk,
            start=start,
            end=end,
            guest=GUEST,
            language="nl",
            actor=Actor.guest(),
        )

    def post_step(self, step, data, url=None):
        payload = {"checkout-current_step": step}
        payload.update({f"{step}-{key}": value for key, value in data.items()})
        return self.client.post(url or self.order_url, payload)

    def checkout(self, start, end, room=None):
        """Go through the three steps; returns the response of every step."""
        stay = self.post_step(
            "stay",
            {"start_date": start, "end_date": end, "room": (room or self.room).pk},
        )
        guest = self.post_step("guest", GUEST)
        confirm = self.post_step("confirm", {"terms": "on"})
        return stay, guest, confirm

    def my_booking(self, order, name="detail"):
        return reverse(f"bookings:{name}", kwargs={"token": order.token})

    def assert_on_stay_step(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["wizard"]["steps"].current, "stay")


class InventoryZeroTests(PageTestCase):
    def test_only_room_type_with_inventory_0_hides_the_hotel(self):
        Room.objects.filter(pk=self.room.pk).update(inventory=0)

        self.assertEqual(self.listed(day(10), day(12)), [])
        # Without a period the hotel is still listed.
        response = self.client.get(self.city_url)
        self.assertEqual(list(response.context["object_list"]), [self.hotel])

        response = self.client.get(self.hotel_url, period(day(10), day(12)))
        self.assertContains(response, "Geen kamers vrij in deze periode")
        self.assertContains(response, "Niet vrij in deze periode")

        response = self.client.get(self.order_url, period(day(10), day(12)))
        self.assertContains(response, "niet te boeken")
        self.checkout(day(10), day(12))
        self.assertFalse(Order.objects.exists())
        with self.assertRaises(RoomUnavailable):
            self.book(day(10), day(12))

    def test_other_room_type_stays_bookable(self):
        Room.objects.filter(pk=self.room.pk).update(inventory=0)
        single = Room.objects.create(
            hotel=self.hotel,
            room_type="Single",
            price=Decimal("80"),
            description="Kamer",
            inventory=2,
        )

        self.assertEqual(self.listed(day(10), day(12)), ["Hotel Oude Delft"])
        self.assertEqual(self.offered_rooms(day(10), day(12)), [single])
        *_, response = self.checkout(day(10), day(12), room=self.room)
        self.assertFalse(Order.objects.exists())
        *_, response = self.checkout(day(10), day(12), room=single)
        self.assertEqual(Order.objects.get().room, single)

    def test_inventory_lowered_to_0_keeps_my_booking_working(self):
        order = self.book(day(10), day(12))
        Room.objects.filter(pk=self.room.pk).update(inventory=0)

        self.assertContains(self.client.get(self.my_booking(order)), "Bevestigd")
        response = self.client.get(self.my_booking(order, "pdf"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b"%PDF"))
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(self.my_booking(order, "cancel"))
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CANCELLED)
        self.assertEqual(len(mail.outbox), 1)
        # Cancelling does not make a room type with inventory 0 bookable.
        self.assertEqual(self.listed(day(10), day(12)), [])


class UnavailableHotelWithBookingTests(PageTestCase):
    """The advice for a hotel with bookings: set it to unavailable, do not delete."""

    def setUp(self):
        super().setUp()
        self.order = self.book(day(10), day(12))
        Hotel.objects.filter(pk=self.hotel.pk).update(is_available=False)

    def test_hotel_cannot_be_booked_any_more(self):
        self.assertEqual(self.listed(day(20), day(22)), [])
        response = self.client.get(self.order_url, period(day(20), day(22)))
        self.assertContains(response, "niet te boeken")
        self.checkout(day(20), day(22))
        self.assertEqual(list(Order.objects.all()), [self.order])
        with self.assertRaises(RoomUnavailable):
            self.book(day(20), day(22))

    def test_my_booking_pages_keep_working(self):
        response = self.client.get(self.my_booking(self.order))
        self.assertContains(response, "Hotel Oude Delft")
        self.assertContains(response, "Bevestigd")
        self.assertContains(response, f'href="{self.my_booking(self.order, "cancel")}"')

        response = self.client.get(self.my_booking(self.order, "pdf"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b"%PDF"))

        response = self.client.get(self.my_booking(self.order, "cancel"))
        self.assertContains(response, "Weet je zeker dat je wilt annuleren?")

    def test_guest_can_still_cancel_for_free(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self.my_booking(self.order, "cancel"))

        self.assertRedirects(
            response, self.my_booking(self.order), fetch_redirect_response=False
        )
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.CANCELLED)
        [cancellation] = mail.outbox
        self.assertIn("geannuleerd", cancellation.subject)

    def test_staff_pages_keep_working(self):
        self.client.force_login(self.staff)
        detail_url = reverse("dashboard:booking_detail", args=[self.order.pk])
        self.assertContains(self.client.get(detail_url), "Hotel Oude Delft")
        response = self.client.post(
            reverse("dashboard:booking_resend", args=[self.order.pk])
        )
        self.assertRedirects(response, detail_url, fetch_redirect_response=False)
        self.assertEqual(len(mail.outbox), 1)
        response = self.client.get(
            reverse("dashboard:booking_list"), {"hotel": self.hotel.pk}
        )
        self.assertEqual(list(response.context["orders"]), [self.order])

    def test_unavailable_room_with_a_booking(self):
        Hotel.objects.filter(pk=self.hotel.pk).update(is_available=True)
        Room.objects.filter(pk=self.room.pk).update(is_available=False)

        self.assertEqual(self.listed(day(20), day(22)), [])
        self.assertContains(self.client.get(self.my_booking(self.order)), "Bevestigd")
        self.assertEqual(
            self.client.get(self.my_booking(self.order, "pdf")).status_code, 200
        )
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(self.my_booking(self.order, "cancel"))
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.CANCELLED)


class StayLengthTests(PageTestCase):
    """At most 30 nights (MAX_NIGHTS): the 31st is refused everywhere."""

    def test_city_page_accepts_30_nights_and_refuses_31(self):
        self.assertEqual(
            self.listed(day(10), day(10) + timedelta(days=30)), [self.hotel.name]
        )

        response = self.client.get(
            self.city_url, period(day(10), day(10) + timedelta(days=31))
        )
        self.assertContains(response, 'role="alert"')
        self.assertContains(response, "Je kunt maximaal 30 nachten boeken.")
        self.assertIsNone(response.context["period"])

    def test_hotel_page_and_checkout_take_over_30_nights_but_not_31(self):
        thirty = period(day(10), day(10) + timedelta(days=30))
        thirty_one = period(day(10), day(10) + timedelta(days=31))

        response = self.client.get(self.hotel_url, thirty)
        self.assertEqual(response.context["period"], (day(10), date(2027, 4, 9)))
        response = self.client.get(self.hotel_url, thirty_one)
        self.assertIsNone(response.context["period"])

        response = self.client.get(self.order_url, thirty)
        self.assertContains(response, 'value="2027-04-09"')
        response = self.client.get(self.order_url, thirty_one)
        self.assertNotContains(response, 'value="2027-04-10"')
        self.assertNotContains(response, 'value="2027-03-10"')

    def test_booking_of_30_nights(self):
        end = day(10) + timedelta(days=30)
        stay, guest, confirm = self.checkout(day(10), end)
        self.assertContains(stay, "× 30 nachten")
        self.assertContains(stay, "€ 3.000,00")

        order = Order.objects.get()
        self.assertRedirects(
            confirm, self.my_booking(order), fetch_redirect_response=False
        )
        self.assertEqual(order.night_count, 30)
        self.assertEqual(order.nights.count(), 30)
        self.assertEqual(order.nights.last().date, date(2027, 4, 8))
        self.assertEqual(order.total_price, Decimal("3000.00"))
        response = self.client.get(self.my_booking(order))
        self.assertContains(response, "30 nachten")
        self.assertContains(response, "€ 3.000,00")
        # The night after the stay is free again.
        self.assertEqual(self.listed(end, end + timedelta(days=1)), [self.hotel.name])

    def test_booking_of_31_nights_is_refused_on_step_1(self):
        stay, guest, confirm = self.checkout(day(10), day(10) + timedelta(days=31))

        self.assert_on_stay_step(stay)
        self.assertContains(stay, "Je kunt maximaal 30 nachten boeken.")
        self.assertFalse(Order.objects.exists())
        with self.assertRaises(ValidationError):
            self.book(day(10), day(10) + timedelta(days=31))


class CheckInTodayTests(PageTestCase):
    def test_guest_can_book_today_but_not_cancel_for_free(self):
        tomorrow = TODAY + timedelta(days=1)
        self.assertEqual(self.listed(TODAY, tomorrow), [self.hotel.name])

        *_, confirm = self.checkout(TODAY, tomorrow)

        order = Order.objects.get()
        self.assertEqual(order.start_date, TODAY)
        response = self.client.get(confirm.url)
        self.assertNotContains(response, self.my_booking(order, "cancel") + '"')
        self.assertContains(
            response, "Kosteloos annuleren kan voor deze boeking niet meer."
        )
        self.client.post(self.my_booking(order, "cancel"))
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CONFIRMED)

        # Staff sees it as an arrival of today.
        self.client.force_login(self.staff)
        response = self.client.get(
            reverse("dashboard:booking_list"), {"snel": "aankomst_vandaag"}
        )
        self.assertEqual(list(response.context["orders"]), [order])
        response = self.client.get(reverse("dashboard:home"))
        self.assertEqual(response.context["arrivals_today"], 1)

    @freeze_time("2027-03-01T23:30:00Z")
    def test_after_midnight_in_amsterdam_today_is_the_next_day(self):
        # 2 March 00:30 in Amsterdam, still 1 March in UTC.
        self.assertEqual(self.listed(day(2), day(3)), [self.hotel.name])
        response = self.client.get(self.city_url, period(day(1), day(2)))
        self.assertContains(response, "Je kunt niet inchecken in het verleden.")

        stay, *_ = self.checkout(day(1), day(2))
        self.assert_on_stay_step(stay)
        self.assertFalse(Order.objects.exists())
        *_, confirm = self.checkout(day(2), day(3))
        order = Order.objects.get()
        self.assertEqual(order.start_date, day(2))

        self.client.force_login(self.staff)
        response = self.client.get(
            reverse("dashboard:booking_list"), {"snel": "aankomst_vandaag"}
        )
        self.assertEqual(list(response.context["orders"]), [order])

    @freeze_time("2027-03-01T22:30:00Z")
    def test_late_in_the_evening_in_amsterdam_today_is_still_today(self):
        # 1 March 23:30 in Amsterdam.
        self.assertEqual(self.listed(day(1), day(2)), [self.hotel.name])
        *_, confirm = self.checkout(day(1), day(2))
        self.assertEqual(Order.objects.get().start_date, day(1))


class BlocksAndBookingsTests(PageTestCase):
    """Inventory 3, two bookings for the 10th and 11th, one room blocked 11-12."""

    def setUp(self):
        super().setUp()
        Room.objects.filter(pk=self.room.pk).update(inventory=3)
        for _ in range(2):
            self.book(day(10), day(12))
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("dashboard:room_blocks", args=[self.room.pk]),
            {
                "start_date": day(11),
                "end_date": day(13),
                "quantity": 1,
                "reason": "Schilderwerk",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.block = RoomBlock.objects.get()
        self.client.logout()

    def test_the_night_with_two_bookings_and_the_block_is_full(self):
        self.assertEqual(self.listed(day(10), day(12)), [])
        self.assertEqual(self.listed(day(11), day(12)), [])
        self.assertEqual(self.offered_rooms(day(10), day(12)), [])
        stay, *_ = self.checkout(day(10), day(12))
        self.assertContains(stay, "Geen kamers vrij in deze periode.")
        with self.assertRaises(RoomUnavailable):
            self.book(day(11), day(12))
        self.assertEqual(Order.objects.count(), 2)

    def test_nights_with_room_left_can_be_booked(self):
        # 10th: two bookings. 12th: only the block. 13th: nothing (half-open).
        for start, end in [(10, 11), (12, 13), (12, 14), (13, 15)]:
            with self.subTest(start=start, end=end):
                self.assertEqual(self.listed(day(start), day(end)), [self.hotel.name])
        self.book(day(10), day(11))
        self.assertEqual(self.listed(day(10), day(11)), [])

    def test_deleting_the_block_frees_the_room(self):
        self.client.force_login(self.staff)
        self.client.post(reverse("dashboard:room_block_delete", args=[self.block.pk]))
        self.client.logout()

        self.assertEqual(self.listed(day(10), day(12)), [self.hotel.name])
        *_, confirm = self.checkout(day(10), day(12))
        self.assertEqual(confirm.status_code, 302)
        self.assertEqual(self.listed(day(10), day(12)), [])

    def test_cancelling_a_booking_frees_a_room_despite_the_block(self):
        order = Order.objects.first()
        cancel_booking(order.pk, actor=Actor.guest())
        self.assertEqual(self.listed(day(10), day(12)), [self.hotel.name])

    def test_block_of_the_whole_inventory_without_bookings(self):
        self.client.force_login(self.staff)
        self.client.post(
            reverse("dashboard:room_blocks", args=[self.room.pk]),
            {"start_date": day(20), "end_date": day(21), "quantity": 3},
        )
        self.assertEqual(self.listed(day(19), day(21)), [])
        self.assertEqual(self.listed(day(19), day(20)), [self.hotel.name])
        self.assertEqual(self.listed(day(21), day(22)), [self.hotel.name])


class PendingHoldOnPagesTests(PageTestCase):
    """A booking that waits for payment occupies the room for its hold only."""

    def test_hold_of_15_minutes(self):
        OrderFactory(
            hotel=self.hotel,
            room=self.room,
            start_date=day(10),
            end_date=day(12),
            status=OrderStatus.PENDING_PAYMENT,
            payment_status=PaymentStatus.OPEN,
            hold_expires_at=datetime(2027, 3, 1, 9, 15, tzinfo=dt_timezone.utc),
        )
        with freeze_time("2027-03-01T09:14:00Z"):
            self.assertEqual(self.listed(day(10), day(12)), [])
            self.assertEqual(self.offered_rooms(day(10), day(12)), [])
        with freeze_time("2027-03-01T09:16:00Z"):
            self.assertEqual(self.listed(day(10), day(12)), [self.hotel.name])
            self.assertEqual(self.offered_rooms(day(10), day(12)), [self.room])


class FullRoomTypeTests(PageTestCase):
    """The default inventory of 5: the sixth booking of a night is refused."""

    def setUp(self):
        super().setUp()
        Room.objects.filter(pk=self.room.pk).update(inventory=5)
        for _ in range(5):
            self.book(day(10), day(11))

    def test_sixth_booking_is_refused(self):
        with self.assertRaises(RoomUnavailable):
            self.book(day(10), day(11))
        self.book(day(11), day(12))
        self.assertEqual(Order.objects.count(), 6)

    def test_hotel_with_only_this_type_disappears_until_a_cancellation(self):
        self.assertEqual(self.listed(day(10), day(11)), [])
        self.assertEqual(self.listed(day(9), day(12)), [])
        self.assertEqual(self.listed(day(11), day(12)), [self.hotel.name])

        cancel_booking(Order.objects.first().pk, actor=Actor.guest())
        self.assertEqual(self.listed(day(10), day(11)), [self.hotel.name])

    def test_hotel_with_another_type_stays_but_the_full_type_disappears(self):
        single = Room.objects.create(
            hotel=self.hotel, room_type="Single", price=Decimal("80"), description="x"
        )
        self.assertEqual(self.listed(day(10), day(11)), [self.hotel.name])
        self.assertEqual(self.offered_rooms(day(10), day(11)), [single])
        response = self.client.get(self.hotel_url, period(day(10), day(11)))
        self.assertEqual(response.context["free_room_ids"], {single.pk})
        self.assertContains(response, "Niet vrij in deze periode", count=1)
