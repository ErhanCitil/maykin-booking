from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from freezegun import freeze_time

from maykinbooking.base.models import City, Hotel, Room
from maykinbooking.base.tests.factories import OrderFactory, RoomBlockFactory

from ..forms import AvailabilityFilterForm, ConfirmForm, GuestForm, StayForm

GUEST_DATA = {
    "first_name": "Alex",
    "last_name": "de Landgraaf",
    "email": "alex@example.com",
    "address": "Keizersgracht 117",
    "zipcode": "1015 cj",
    "country": "NL",
}


class StayFormTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        city = City.objects.create(city_id="AMS", name="Amsterdam")
        cls.hotel = Hotel.objects.create(city=city, hotel_id="AMS01", name="Ibis")
        cls.single = Room.objects.create(
            hotel=cls.hotel, room_type="Single", price=Decimal("80"), description="x"
        )
        cls.double = Room.objects.create(
            hotel=cls.hotel, room_type="Double", price=Decimal("100"), description="x"
        )
        cls.closed = Room.objects.create(
            hotel=cls.hotel,
            room_type="Family",
            price=Decimal("150"),
            description="x",
            is_available=False,
        )
        other_hotel = Hotel.objects.create(city=city, hotel_id="AMS02", name="Novotel")
        cls.other_room = Room.objects.create(
            hotel=other_hotel, room_type="Single", price=Decimal("60"), description="x"
        )

    def make_form(self, start_offset=1, nights=2, room=None):
        start = timezone.localdate() + timedelta(days=start_offset)
        return StayForm(
            data={
                "start_date": start,
                "end_date": start + timedelta(days=nights),
                "room": (room or self.double).pk,
            },
            hotel=self.hotel,
        )

    def test_valid_stay(self):
        self.assertTrue(self.make_form().is_valid())

    def test_check_in_today_is_allowed(self):
        self.assertTrue(self.make_form(start_offset=0).is_valid())

    def test_check_in_in_the_past(self):
        form = self.make_form(start_offset=-1)
        self.assertFalse(form.is_valid())
        self.assertIn("start_date", form.errors)

    def test_check_out_must_be_after_check_in(self):
        form = self.make_form(nights=0)
        self.assertFalse(form.is_valid())
        self.assertIn("end_date", form.errors)

    def test_at_most_30_nights(self):
        self.assertTrue(self.make_form(nights=30).is_valid())
        form = self.make_form(nights=31)
        self.assertFalse(form.is_valid())
        self.assertIn("end_date", form.errors)

    def test_only_available_rooms_of_this_hotel(self):
        form = StayForm(hotel=self.hotel)
        self.assertEqual(list(form.fields["room"].queryset), [self.single, self.double])
        self.assertFalse(self.make_form(room=self.closed).is_valid())
        self.assertFalse(self.make_form(room=self.other_room).is_valid())

    def test_cheapest_room_is_preselected(self):
        form = StayForm(hotel=self.hotel)
        self.assertEqual(form.fields["room"].initial, self.single.pk)

    def test_rooms_without_inventory_are_not_offered(self):
        Room.objects.filter(pk=self.single.pk).update(inventory=0)
        form = StayForm(hotel=self.hotel)
        self.assertEqual(list(form.fields["room"].queryset), [self.double])

    def test_date_inputs_use_iso_dates(self):
        # <input type="date"> only understands YYYY-MM-DD.
        form = StayForm(
            hotel=self.hotel,
            initial={"start_date": date(2027, 3, 10), "end_date": date(2027, 3, 12)},
        )
        self.assertIn('value="2027-03-10"', str(form["start_date"]))
        self.assertIn('value="2027-03-12"', str(form["end_date"]))

    @freeze_time("2027-03-09T23:30:00Z")
    def test_today_is_the_local_date(self):
        # 00:30 on 10 March in Amsterdam, while it is still 9 March in UTC.
        form = StayForm(
            data={
                "start_date": date(2027, 3, 9),
                "end_date": date(2027, 3, 11),
                "room": self.double.pk,
            },
            hotel=self.hotel,
        )
        self.assertFalse(form.is_valid())
        self.assertEqual(
            form.errors["start_date"], ["Je kunt niet inchecken in het verleden."]
        )
        self.assertEqual(form.fields["start_date"].widget.attrs["min"], "2027-03-10")
        form = StayForm(
            data={
                "start_date": date(2027, 3, 10),
                "end_date": date(2027, 3, 11),
                "room": self.double.pk,
            },
            hotel=self.hotel,
        )
        self.assertTrue(form.is_valid(), form.errors)


class StayFormAvailabilityTests(TestCase):
    """The rooms that can be chosen depend on the period (US1.2)."""

    @classmethod
    def setUpTestData(cls):
        city = City.objects.create(city_id="AMS", name="Amsterdam")
        cls.hotel = Hotel.objects.create(city=city, hotel_id="AMS01", name="Ibis")
        cls.single = Room.objects.create(
            hotel=cls.hotel,
            room_type="Single",
            price=Decimal("80"),
            description="x",
            inventory=1,
        )
        cls.double = Room.objects.create(
            hotel=cls.hotel,
            room_type="Double",
            price=Decimal("100"),
            description="x",
            inventory=1,
        )

    def setUp(self):
        self.start = timezone.localdate() + timedelta(days=10)
        self.end = self.start + timedelta(days=2)

    def book(self, room, start=None, end=None):
        return OrderFactory(
            hotel=self.hotel,
            room=room,
            start_date=start or self.start,
            end_date=end or self.end,
        )

    def bound_form(self, room, start=None, end=None):
        return StayForm(
            data={
                "start_date": start or self.start,
                "end_date": end or self.end,
                "room": room.pk,
            },
            hotel=self.hotel,
        )

    def test_queryset_holds_the_free_rooms_of_the_period(self):
        self.book(self.single)
        form = self.bound_form(self.double)
        self.assertEqual(list(form.fields["room"].queryset), [self.double])
        self.assertTrue(form.is_valid(), form.errors)

    def test_initial_period_filters_the_rooms(self):
        self.book(self.single)
        form = StayForm(
            hotel=self.hotel, initial={"start_date": self.start, "end_date": self.end}
        )
        self.assertEqual(list(form.fields["room"].queryset), [self.double])
        self.assertEqual(form.fields["room"].initial, self.double.pk)
        self.assertFalse(form.no_rooms_available)

    def test_without_period_all_available_rooms_are_offered(self):
        self.book(self.single)
        form = StayForm(hotel=self.hotel)
        self.assertEqual(list(form.fields["room"].queryset), [self.single, self.double])
        self.assertFalse(form.no_rooms_available)

    def test_room_that_is_not_free_is_rejected(self):
        self.book(self.single)
        form = self.bound_form(self.single)
        self.assertFalse(form.is_valid())
        self.assertEqual(
            form.errors["room"],
            ["Deze kamer is niet vrij in deze periode. Kies een andere kamer."],
        )
        self.assertEqual(form.non_field_errors(), [])

    def test_room_is_free_again_on_the_day_of_departure(self):
        self.book(self.single)
        form = self.bound_form(self.single, self.end, self.end + timedelta(days=1))
        self.assertTrue(form.is_valid(), form.errors)

    def test_blocked_room_is_rejected(self):
        RoomBlockFactory(room=self.single, start_date=self.start, end_date=self.end)
        self.assertFalse(self.bound_form(self.single).is_valid())

    def test_no_free_rooms_is_a_form_error(self):
        self.book(self.single)
        self.book(self.double)
        form = self.bound_form(self.double)
        self.assertFalse(form.is_valid())
        self.assertEqual(form.non_field_errors(), ["Geen kamers vrij in deze periode."])
        self.assertNotIn("room", form.errors)
        self.assertTrue(form.no_rooms_available)

    def test_no_free_rooms_for_the_initial_period(self):
        self.book(self.single)
        self.book(self.double)
        form = StayForm(
            hotel=self.hotel, initial={"start_date": self.start, "end_date": self.end}
        )
        self.assertTrue(form.no_rooms_available)
        self.assertEqual(list(form.fields["room"].queryset), [])

    def test_invalid_period_does_not_filter_the_rooms(self):
        self.book(self.single)
        form = self.bound_form(self.single, self.start, self.start)
        self.assertEqual(list(form.fields["room"].queryset), [self.single, self.double])
        self.assertFalse(form.is_valid())
        self.assertEqual(
            form.errors["end_date"], ["Uitchecken moet na de incheckdatum zijn."]
        )
        self.assertNotIn("room", form.errors)

    def test_cancelled_order_does_not_occupy_the_room(self):
        order = self.book(self.single)
        order.status = "cancelled"
        order.save()
        self.assertTrue(self.bound_form(self.single).is_valid())


class AvailabilityFilterFormTests(TestCase):
    def setUp(self):
        self.start = timezone.localdate() + timedelta(days=3)

    def make_form(self, van="", tot=""):
        return AvailabilityFilterForm(data={"van": van, "tot": tot})

    def test_no_dates_is_no_filter(self):
        form = self.make_form()
        self.assertTrue(form.is_valid())
        self.assertIsNone(form.period)
        self.assertIsNone(AvailabilityFilterForm().period)

    def test_valid_period(self):
        end = self.start + timedelta(days=2)
        form = self.make_form(self.start.isoformat(), end.isoformat())
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.period, (self.start, end))

    def test_both_dates_are_needed(self):
        for data in [{"van": self.start.isoformat()}, {"tot": self.start.isoformat()}]:
            with self.subTest(data=data):
                form = self.make_form(**data)
                self.assertFalse(form.is_valid())
                self.assertEqual(
                    form.non_field_errors(),
                    ["Vul zowel een incheck- als een uitcheckdatum in."],
                )
                self.assertIsNone(form.period)

    def test_invalid_periods(self):
        today = timezone.localdate()
        cases = [
            (today - timedelta(days=1), today + timedelta(days=1), "van"),
            (self.start, self.start, "tot"),
            (self.start, self.start - timedelta(days=1), "tot"),
            (self.start, self.start + timedelta(days=31), "tot"),
            # More than 365 days ahead: the error is about the check-in date.
            (today + timedelta(days=366), today + timedelta(days=368), "van"),
        ]
        for start, end, field in cases:
            with self.subTest(start=start, end=end):
                form = self.make_form(start.isoformat(), end.isoformat())
                self.assertFalse(form.is_valid())
                self.assertIn(field, form.errors)
                self.assertIsNone(form.period)

    def test_date_pickers_stop_at_the_booking_horizon(self):
        today = timezone.localdate()
        form = AvailabilityFilterForm()
        self.assertEqual(form.fields["van"].widget.attrs["min"], today.isoformat())
        self.assertEqual(
            form.fields["van"].widget.attrs["max"],
            (today + timedelta(days=365)).isoformat(),
        )
        self.assertEqual(
            form.fields["tot"].widget.attrs["max"],
            (today + timedelta(days=395)).isoformat(),
        )

    def test_unreadable_date(self):
        form = self.make_form("morgen", self.start.isoformat())
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["van"], ["Vul een geldige incheckdatum in."])
        self.assertIsNone(form.period)


class GuestFormTests(TestCase):
    def test_form_has_fields(self):
        expected = ["first_name", "last_name", "email", "address", "zipcode", "country"]
        self.assertSequenceEqual(expected, list(GuestForm().fields))

    def test_all_fields_are_required(self):
        form = GuestForm(data={})
        self.assertFalse(form.is_valid())
        self.assertEqual(set(form.errors), set(GuestForm().fields))

    def test_zipcode_is_normalised(self):
        form = GuestForm(data=GUEST_DATA)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["zipcode"], "1015CJ")

    def test_zipcode_too_long(self):
        form = GuestForm(data={**GUEST_DATA, "zipcode": "1234567"})
        self.assertFalse(form.is_valid())
        self.assertIn("zipcode", form.errors)

    def test_invalid_email(self):
        form = GuestForm(data={**GUEST_DATA, "email": "geen-email"})
        self.assertFalse(form.is_valid())
        self.assertIn("email", form.errors)

    def test_line_breaks_and_special_characters_are_refused(self):
        """Security S2: nothing that changes how a mail, PDF or CSV reads."""
        cases = [
            ("first_name", "Alex\r\nBcc: iedereen@example.com"),
            ("last_name", "Landgraaf\u202egnp.exe"),  # right-to-left override
            ("address", "Keizersgracht\n117"),
            ("address", "Keizersgracht\u2028117"),  # line separator
            ("zipcode", "1015\u202eCJ"),
            ("zipcode", "10\r\n15"),
        ]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                form = GuestForm(data={**GUEST_DATA, field: value})
                self.assertFalse(form.is_valid())
                self.assertEqual(
                    form.errors[field],
                    ["Gebruik geen regeleinden of speciale tekens."],
                )

    def test_names_with_accents_and_punctuation_are_fine(self):
        form = GuestForm(
            data={
                **GUEST_DATA,
                "first_name": "Zoë-Anne",
                "last_name": "d'Hondt van Ærø",
                "address": "Rue de l'Église 3, bus 2",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_surrounding_whitespace_is_stripped_not_refused(self):
        form = GuestForm(data={**GUEST_DATA, "first_name": " Alex\r\n"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["first_name"], "Alex")


class ConfirmFormTests(TestCase):
    def test_terms_must_be_accepted(self):
        self.assertFalse(ConfirmForm(data={}).is_valid())
        self.assertTrue(ConfirmForm(data={"terms": "on"}).is_valid())
