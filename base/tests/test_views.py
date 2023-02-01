from django.test import TestCase
from .factories import HotelFactory, RoomFactory, CityFactory, HighlightFactory
from django.urls import reverse

class TestViews(TestCase):
    def setUp(self):
        self.city = CityFactory()
        self.highlight = HighlightFactory(name='Free wifi')
        self.hotel = HotelFactory(highlight=[self.highlight])
        self.room = RoomFactory(hotel=self.hotel)

    def test_hotel_detail(self):
        response = self.client.get(reverse('hotel_detail', args=[self.hotel.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed('hotel.html')
    
    def test_highlight_hotel(self):
        response = self.client.get(reverse('hotel_detail', args=[self.highlight.id]))
        self.assertContains(response, self.highlight.name)

    def test_hotel_edit_page(self):
        response = self.client.get(reverse('hotel_edit', args=[self.hotel.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed('hotel_edit.html')