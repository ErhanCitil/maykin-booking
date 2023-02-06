from django.test import TestCase
from .factories import HotelFactory, RoomFactory, CityFactory, HighlightFactory, OrderFactory
from django.urls import reverse
from django.contrib.auth.models import User
from django_webtest import WebTest

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
        self.assertEqual(response.status_code, 302)
        self.assertTemplateUsed('hotel_edit.html')

class TestLogin(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='Alex', password='Landgraaf123')
        self.city = CityFactory()
        self.highlight = HighlightFactory(name='Free renting a bike')
        self.hotel = HotelFactory(highlight=[self.highlight])
        self.room = RoomFactory(hotel=self.hotel)

    def test_login(self):
        response = self.client.get(reverse('login'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed('login.html')

    def test_hotel_edit_page_redirects_to_login(self):
        # Hier test ik dat de gebruiker word doorgestuurd als diegene niet is ingelogd dus de gebruiker kan de edit pagina niet bereiken als hij/zij niet is ingelogd.
        response = self.client.get(reverse('hotel_edit', args=[self.hotel.id]))
        self.assertRedirects(response, '/login/?next=/hotel_edit/{}'.format(self.hotel.id))

    def test_user_logged_in(self):
        self.client.login(username='Alex', password='Landgraaf123')
        response = self.client.get(reverse('hotel_edit', args=[self.hotel.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed('hotel_edit.html')

class EditPage(WebTest):
    def setUp(self):
        self.user = User.objects.create_user(username='Alex', password='Landgraaf123')
        self.city = CityFactory()
        self.highlight = HighlightFactory(name='Free renting a bike')
        self.hotel = HotelFactory(highlight=[self.highlight], name="Alex Hotel", price=100.00)
        self.room = RoomFactory(hotel=self.hotel)

    def test_user_change_details(self):
        self.client.login(username='Alex', password='Landgraaf123')
        response = self.app.get(reverse('hotel_edit', args=[self.hotel.id]), user='Alex').form
        response['name'] = 'Hotel'
        response['price'] = 200
        response.submit()
        self.assertEqual(response['name'].value, 'Hotel')
        self.assertEqual(response['price'].value, 200)

class OrderPage(TestCase):
    def setUp(self):
        self.city = CityFactory()
        self.highlight = HighlightFactory(name='Free WiFi')
        self.hotel = HotelFactory(highlight=[self.highlight], city = self.city)
        self.room = RoomFactory(room_type='Single')
        self.order = OrderFactory(hotel=self.hotel, room=self.room)

    def test_order_page(self):
        response = self.client.get(reverse('order', args=[self.order.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed('order.html')