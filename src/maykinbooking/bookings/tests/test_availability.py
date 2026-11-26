from datetime import date, datetime, timedelta, timezone as dt_timezone

from django.core.exceptions import ValidationError
from django.db.models import Min
from django.test import TestCase

from freezegun import freeze_time

from maykinbooking.base.models import Hotel, OrderStatus, Room
from maykinbooking.base.tests.factories import (
    CityFactory,
    HotelFactory,
    OrderFactory,
    RoomBlockFactory,
    RoomFactory,
)

from ..services.availability import (
    MAX_DAYS_AHEAD,
    MAX_NIGHTS,
    available_hotel_ids,
    available_rooms,
    free_counts,
    occupancy,
    overbooked_nights,
    validate_stay_period,
)


def day(number):
    """A day in a month far enough away; availability itself has no 'today'."""
    return date(2030, 6, 1) + timedelta(days=number - 1)


class AvailabilityTestCase(TestCase):
    def make_room(self, **fields):
        fields.setdefault("is_available", True)
        fields.setdefault("hotel", HotelFactory(is_available=True))
        return RoomFactory(**fields)

    def book(self, room, start, end, **fields):
        return OrderFactory(
            room=room,
            hotel=room.hotel,
            start_date=day(start),
            end_date=day(end),
            **fields
        )

    def is_free(self, room, start, end, *, now=None):
        room.refresh_from_db()
        return free_counts([room], day(start), day(end), now=now)[room.pk] >= 1


class OverlapTests(AvailabilityTestCase):
    """Testpunt 1: inventory 1, a booking for [10, 12)."""

    def setUp(self):
        self.room = self.make_room(inventory=1)
        self.book(self.room, 10, 12)

    def test_stays_that_do_not_overlap_are_free(self):
        self.assertTrue(self.is_free(self.room, 12, 14))  # arrives when it ends
        self.assertTrue(self.is_free(self.room, 9, 10))  # leaves when it starts

    def test_overlapping_stays_are_full(self):
        self.assertFalse(self.is_free(self.room, 11, 12))
        self.assertFalse(self.is_free(self.room, 9, 13))
        self.assertFalse(self.is_free(self.room, 10, 12))

    def test_occupancy_per_night(self):
        self.assertEqual(
            occupancy([self.room.pk], day(9), day(13)),
            {self.room.pk: {day(10): 1, day(11): 1}},
        )


class PerNightTests(AvailabilityTestCase):
    """Testpunt 2: orders [10, 11) and [11, 12) never share a night."""

    def test_inventory_one_is_full(self):
        room = self.make_room(inventory=1)
        self.book(room, 10, 11)
        self.book(room, 11, 12)
        self.assertFalse(self.is_free(room, 10, 12))

    def test_inventory_two_has_one_left(self):
        room = self.make_room(inventory=2)
        self.book(room, 10, 11)
        self.book(room, 11, 12)
        self.assertEqual(free_counts([room], day(10), day(12)), {room.pk: 1})

    def test_free_count_is_inventory_minus_the_busiest_night(self):
        room = self.make_room(inventory=5)
        self.book(room, 10, 13)
        self.book(room, 11, 12)
        self.book(room, 11, 15)
        self.assertEqual(free_counts([room], day(10), day(15)), {room.pk: 2})


class WhatCountsTests(AvailabilityTestCase):
    """Testpunt 3: which orders and blocks occupy a room."""

    def setUp(self):
        self.room = self.make_room(inventory=1)

    def test_confirmed_order_counts(self):
        self.book(self.room, 10, 12, status=OrderStatus.CONFIRMED)
        self.assertFalse(self.is_free(self.room, 10, 12))

    def test_pending_order_counts_while_its_hold_lasts(self):
        booked_at = datetime(2030, 5, 1, 12, 0, tzinfo=dt_timezone.utc)
        self.book(
            self.room,
            10,
            12,
            status=OrderStatus.PENDING_PAYMENT,
            hold_expires_at=booked_at + timedelta(minutes=15),
        )
        after_14_minutes = booked_at + timedelta(minutes=14)
        after_16_minutes = booked_at + timedelta(minutes=16)
        self.assertFalse(self.is_free(self.room, 10, 12, now=after_14_minutes))
        self.assertTrue(self.is_free(self.room, 10, 12, now=after_16_minutes))

    def test_pending_order_without_hold_does_not_count(self):
        self.book(self.room, 10, 12, status=OrderStatus.PENDING_PAYMENT)
        self.assertTrue(self.is_free(self.room, 10, 12))

    def test_cancelled_and_expired_orders_do_not_count(self):
        self.book(self.room, 10, 12, status=OrderStatus.CANCELLED)
        self.book(self.room, 10, 12, status=OrderStatus.EXPIRED)
        self.assertTrue(self.is_free(self.room, 10, 12))

    def test_room_block_counts_with_its_quantity(self):
        self.room.inventory = 3
        self.room.save()
        RoomBlockFactory(
            room=self.room, start_date=day(11), end_date=day(13), quantity=2
        )
        self.assertEqual(free_counts([self.room], day(10), day(12)), {self.room.pk: 1})
        self.book(self.room, 12, 13)
        self.assertEqual(free_counts([self.room], day(10), day(14)), {self.room.pk: 0})
        # The block ends on day 13: that night is free again.
        self.assertEqual(free_counts([self.room], day(13), day(14)), {self.room.pk: 3})
        self.assertEqual(
            occupancy([self.room.pk], day(10), day(14)),
            {self.room.pk: {day(11): 2, day(12): 3}},
        )

    def test_block_of_another_room_does_not_count(self):
        RoomBlockFactory(start_date=day(10), end_date=day(12))
        self.assertTrue(self.is_free(self.room, 10, 12))

    def test_inventory_zero_is_never_free(self):
        room = self.make_room(inventory=0)
        self.assertFalse(self.is_free(room, 10, 12))
        self.assertFalse(
            available_rooms(Room.objects.all(), day(10), day(12))
            .filter(pk=room.pk)
            .exists()
        )

    def test_unavailable_room_or_hotel_is_left_out(self):
        unavailable_room = self.make_room(is_available=False)
        closed_hotel_room = self.make_room(hotel=HotelFactory(is_available=False))
        rooms = available_rooms(Room.objects.all(), day(10), day(12))
        self.assertEqual(list(rooms), [self.room])
        self.assertNotIn(unavailable_room, rooms)
        self.assertNotIn(closed_hotel_room, rooms)

    def test_overbooked_room_has_zero_free(self):
        self.book(self.room, 10, 12)
        self.book(self.room, 10, 12)
        self.assertEqual(free_counts([self.room], day(10), day(12)), {self.room.pk: 0})


class AvailableRoomsTests(AvailabilityTestCase):
    def test_keeps_ordering_and_select_related_of_the_queryset(self):
        hotel = HotelFactory(is_available=True)
        cheap = self.make_room(hotel=hotel, price=50, inventory=1)
        expensive = self.make_room(hotel=hotel, price=150)
        full = self.make_room(hotel=hotel, price=100, inventory=1)
        self.book(full, 10, 12)
        self.book(cheap, 1, 3)  # other dates

        rooms = available_rooms(
            hotel.room.select_related("hotel").order_by("-price"), day(10), day(12)
        )
        with self.assertNumQueries(1):
            self.assertEqual(
                [(room.pk, room.hotel.pk) for room in rooms],
                [(expensive.pk, hotel.pk), (cheap.pk, hotel.pk)],
            )

    def test_three_queries(self):
        hotel = HotelFactory(is_available=True)
        for _ in range(3):
            self.make_room(hotel=hotel)
        with self.assertNumQueries(3):
            available_rooms(hotel.room.all(), day(10), day(12))


class AvailableHotelIdsTests(AvailabilityTestCase):
    def setUp(self):
        self.city = CityFactory()

    def make_hotels(self, count):
        hotels = []
        for _ in range(count):
            hotel = HotelFactory(city=self.city, is_available=True)
            room = self.make_room(hotel=hotel, inventory=1)
            self.book(room, 10, 12)
            self.make_room(hotel=hotel, inventory=2)
            RoomBlockFactory(room=room, start_date=day(1), end_date=day(30))
            hotels.append(hotel)
        return hotels

    def test_hotels_with_a_free_room(self):
        free_hotel = HotelFactory(city=self.city, is_available=True)
        self.make_room(hotel=free_hotel, inventory=1)
        full_hotel = HotelFactory(city=self.city, is_available=True)
        self.book(self.make_room(hotel=full_hotel, inventory=1), 9, 13)
        closed_hotel = HotelFactory(city=self.city, is_available=False)
        self.make_room(hotel=closed_hotel)
        without_rooms = HotelFactory(city=self.city, is_available=True)
        other_city = self.make_room().hotel

        ids = available_hotel_ids(
            Hotel.objects.filter(city=self.city), day(10), day(12)
        )

        self.assertEqual(ids, {free_hotel.pk})
        self.assertNotIn(without_rooms.pk, ids)
        self.assertNotIn(other_city.pk, ids)

    def test_works_with_the_annotated_city_page_queryset(self):
        hotel = self.make_hotels(1)[0]
        hotels = (
            Hotel.objects.filter(city=self.city)
            .annotate(min_price=Min("room__price"))
            .order_by("name")
        )
        self.assertEqual(available_hotel_ids(hotels, day(10), day(12)), {hotel.pk})

    def test_number_of_queries_does_not_depend_on_the_number_of_hotels(self):
        for count in [2, 10]:
            with self.subTest(hotels=count):
                Hotel.objects.filter(city=self.city).update(is_available=False)
                hotels = self.make_hotels(count)
                with self.assertNumQueries(3):
                    ids = available_hotel_ids(
                        Hotel.objects.filter(city=self.city), day(10), day(12)
                    )
                self.assertEqual(ids, {hotel.pk for hotel in hotels})


class OverbookedNightsTests(AvailabilityTestCase):
    def setUp(self):
        self.room = self.make_room(inventory=2)
        self.book(self.room, 10, 12)
        self.book(self.room, 11, 13)
        RoomBlockFactory(room=self.room, start_date=day(20), end_date=day(21))

    def test_no_overbooking_with_the_current_inventory(self):
        self.assertEqual(overbooked_nights(self.room, from_date=day(1)), [])

    def test_lower_inventory(self):
        self.assertEqual(
            overbooked_nights(self.room, inventory=1, from_date=day(1)),
            [(day(11), 2)],
        )
        self.assertEqual(
            overbooked_nights(self.room, inventory=0, from_date=day(1)),
            [(day(10), 1), (day(11), 2), (day(12), 1), (day(20), 1)],
        )

    def test_only_from_the_given_date(self):
        self.assertEqual(
            overbooked_nights(self.room, inventory=0, from_date=day(12)),
            [(day(12), 1), (day(20), 1)],
        )

    def test_defaults_to_from_today(self):
        with freeze_time(datetime(2030, 6, 11, 22, 30, tzinfo=dt_timezone.utc)):
            # 00:30 on day 12 in Amsterdam: day 12 is "today".
            self.assertEqual(
                overbooked_nights(self.room, inventory=0),
                [(day(12), 1), (day(20), 1)],
            )

    def test_room_without_orders_or_blocks(self):
        self.assertEqual(overbooked_nights(self.make_room(), inventory=0), [])


class ValidateStayPeriodTests(TestCase):
    today = date(2027, 3, 1)

    def assertInvalid(self, start, end, code):
        with self.assertRaises(ValidationError) as error:
            validate_stay_period(start, end, today=self.today)
        self.assertEqual(error.exception.code, code)

    def test_valid_periods(self):
        validate_stay_period(
            self.today, self.today + timedelta(days=1), today=self.today
        )
        validate_stay_period(
            self.today, self.today + timedelta(days=MAX_NIGHTS), today=self.today
        )

    def test_start_in_the_past(self):
        self.assertInvalid(self.today - timedelta(days=1), self.today, "past")

    def test_end_not_after_start(self):
        self.assertInvalid(self.today, self.today, "end_before_start")
        self.assertInvalid(
            self.today + timedelta(days=2),
            self.today + timedelta(days=1),
            "end_before_start",
        )

    def test_too_long(self):
        self.assertInvalid(
            self.today, self.today + timedelta(days=MAX_NIGHTS + 1), "too_long"
        )

    def test_booking_horizon_of_365_days(self):
        self.assertEqual(MAX_DAYS_AHEAD, 365)
        last_arrival = self.today + timedelta(days=365)
        validate_stay_period(
            last_arrival, last_arrival + timedelta(days=MAX_NIGHTS), today=self.today
        )
        too_far = last_arrival + timedelta(days=1)
        self.assertInvalid(too_far, too_far + timedelta(days=2), "too_far")

    def test_message_is_dutch(self):
        with self.assertRaises(ValidationError) as error:
            validate_stay_period(
                self.today, self.today + timedelta(days=31), today=self.today
            )
        self.assertEqual(
            error.exception.messages, ["Je kunt maximaal 30 nachten boeken."]
        )
