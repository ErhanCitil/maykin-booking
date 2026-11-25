from datetime import date, datetime, timedelta

from django.test import TestCase
from django.urls import reverse

from maykinbooking.base.models import City, Hotel, Order, Room
from maykinbooking.base.tests import factories


class TestOrder(TestCase):
    def setUp(self):
        self.hotel = factories.HotelFactory()
        self.room = factories.RoomFactory(
            hotel=self.hotel, room_type="Single", price=100.00
        )
        self.order = factories.OrderFactory(
            room=self.room,
            hotel=self.hotel,
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 3),
        )

    def test_order_property_total_price(self):
        self.assertEqual(self.order.total_price, 200.00)
