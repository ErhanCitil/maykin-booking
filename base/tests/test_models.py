from django.test import TestCase
from .factories import CityFactory, HotelFactory, RoomFactory
# Create your tests here.

class ModelTest(TestCase):
    def setUp(self):
        self.city = CityFactory(city_id="AMS", name="Amsterdam")
        self.hotel = HotelFactory(city=self.city, hotel_id="HOTEL1", name="Hotel 1", price=100.00, is_available=True)
        self.room = RoomFactory()

    def test_city(self):
        self.assertEqual(self.city.city_id, "AMS")
        self.assertEqual(self.city.name, "Amsterdam")

    def test_hotel(self):
        self.assertEqual(self.hotel.city, self.city)
        self.assertEqual(self.hotel.hotel_id, "HOTEL1")
        self.assertEqual(self.hotel.name, "Hotel 1")
        self.assertEqual(self.hotel.price, 100.00)
        self.assertEqual(self.hotel.is_available, True)

    def test_room(self):
        self.assertEqual(self.room.hotel, self.hotel)
        self.assertEqual(self.room.title, "Room 1")
        self.assertEqual(self.room.price, 100.00)