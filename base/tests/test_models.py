from django.test import TestCase
from .factories import CityFactory, HotelFactory, RoomFactory
# Create your tests here.

class ModelTest(TestCase):
    def setUp(self):
        self.city = CityFactory()
        self.hotel = HotelFactory()
        self.room = RoomFactory()

    def test_city(self):
        self.city.city_id = "AMS"
        self.city.name = "Amsterdam"
        self.assertEqual(self.city.city_id, "AMS")
        self.assertEqual(self.city.name, "Amsterdam")

    def test_hotel(self):
        self.hotel.city = self.city
        self.hotel.name = "Hotel 1"
        self.assertEqual(self.hotel.city, self.city)
        self.assertEqual(self.hotel.name, "Hotel 1")

    def test_room(self):
        self.room.hotel = self.hotel
        self.room.title = "Room 1"
        self.room.price = 100.00
        self.assertEqual(self.room.hotel, self.hotel)
        self.assertEqual(self.room.title, "Room 1")
        self.assertEqual(self.room.price, 100.00)