from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase

from maykinbooking.base.models import City, Hotel, Room

from ..forms import ConfirmForm, GuestForm, StayForm

GUEST_DATA = {
    'first_name': 'Alex',
    'last_name': 'de Landgraaf',
    'email': 'alex@example.com',
    'address': 'Keizersgracht 117',
    'zipcode': '1015 cj',
    'country': 'NL',
}


class StayFormTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        city = City.objects.create(city_id='AMS', name='Amsterdam')
        cls.hotel = Hotel.objects.create(city=city, hotel_id='AMS01', name='Ibis')
        cls.single = Room.objects.create(
            hotel=cls.hotel, room_type='Single', price=Decimal('80'), description='x'
        )
        cls.double = Room.objects.create(
            hotel=cls.hotel, room_type='Double', price=Decimal('100'), description='x'
        )
        cls.closed = Room.objects.create(
            hotel=cls.hotel, room_type='Family', price=Decimal('150'), description='x',
            is_available=False,
        )
        other_hotel = Hotel.objects.create(city=city, hotel_id='AMS02', name='Novotel')
        cls.other_room = Room.objects.create(
            hotel=other_hotel, room_type='Single', price=Decimal('60'), description='x'
        )

    def make_form(self, start_offset=1, nights=2, room=None):
        start = date.today() + timedelta(days=start_offset)
        return StayForm(
            data={
                'start_date': start,
                'end_date': start + timedelta(days=nights),
                'room': (room or self.double).pk,
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
        self.assertIn('start_date', form.errors)

    def test_check_out_must_be_after_check_in(self):
        form = self.make_form(nights=0)
        self.assertFalse(form.is_valid())
        self.assertIn('end_date', form.errors)

    def test_at_most_30_nights(self):
        self.assertTrue(self.make_form(nights=30).is_valid())
        form = self.make_form(nights=31)
        self.assertFalse(form.is_valid())
        self.assertIn('end_date', form.errors)

    def test_only_available_rooms_of_this_hotel(self):
        form = StayForm(hotel=self.hotel)
        self.assertEqual(
            list(form.fields['room'].queryset), [self.single, self.double]
        )
        self.assertFalse(self.make_form(room=self.closed).is_valid())
        self.assertFalse(self.make_form(room=self.other_room).is_valid())

    def test_cheapest_room_is_preselected(self):
        form = StayForm(hotel=self.hotel)
        self.assertEqual(form.fields['room'].initial, self.single.pk)


class GuestFormTests(TestCase):
    def test_form_has_fields(self):
        expected = ['first_name', 'last_name', 'email', 'address', 'zipcode', 'country']
        self.assertSequenceEqual(expected, list(GuestForm().fields))

    def test_all_fields_are_required(self):
        form = GuestForm(data={})
        self.assertFalse(form.is_valid())
        self.assertEqual(set(form.errors), set(GuestForm().fields))

    def test_zipcode_is_normalised(self):
        form = GuestForm(data=GUEST_DATA)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['zipcode'], '1015CJ')

    def test_zipcode_too_long(self):
        form = GuestForm(data={**GUEST_DATA, 'zipcode': '1234567'})
        self.assertFalse(form.is_valid())
        self.assertIn('zipcode', form.errors)

    def test_invalid_email(self):
        form = GuestForm(data={**GUEST_DATA, 'email': 'geen-email'})
        self.assertFalse(form.is_valid())
        self.assertIn('email', form.errors)


class ConfirmFormTests(TestCase):
    def test_terms_must_be_accepted(self):
        self.assertFalse(ConfirmForm(data={}).is_valid())
        self.assertTrue(ConfirmForm(data={'terms': 'on'}).is_valid())
