from django.test import TestCase
from ..models import City, Hotel, Room, Order
from django.urls import reverse
from base.tests import factories
from datetime import timedelta, datetime, date
from django_webtest import WebTest
from form.forms import OrderForm1, OrderForm2

class WebTest(WebTest):
    def setUp(self):
        self.hotel = factories.HotelFactory()
        self.room = factories.RoomFactory(hotel=self.hotel, room_type='Single', price=100.00)

    def test_orderform1(self):
        form = OrderForm1(data={'start_date': date.today(), 'end_date': date.today() + timedelta(days=1), 'room_type': 'Single'})
        self.assertTrue(form.is_valid())