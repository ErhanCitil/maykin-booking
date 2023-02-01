from django.test import TestCase
from django.urls import reverse
from base.models import Hotel, Room, Order, City
from base.tests import factories
from datetime import timedelta, datetime, date

class TestOrder(TestCase):
    def setUp(self):
        self.hotel = factories.HotelFactory()
        self.room = factories.RoomFactory(hotel=self.hotel, room_type='Single', price=100.00)
        self.order = factories.OrderFactory(room=self.room, hotel=self.hotel, start_date=date(2023, 1, 1), end_date=date(2023, 1, 3))
    
    def test_order_property_total_price(self):
        self.assertEqual(self.order.total_price, 200.00)