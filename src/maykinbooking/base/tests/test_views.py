from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from django_webtest import WebTest

from .factories import (
    CityFactory,
    HighlightFactory,
    HotelFactory,
    OrderFactory,
    RoomFactory,
)


class TestViews(TestCase):
    def setUp(self):
        self.city = CityFactory()
        self.highlight = HighlightFactory(name="Free wifi")
        self.hotel = HotelFactory(highlight=[self.highlight])
        self.room = RoomFactory(hotel=self.hotel)

    def get_detail_page(self, hotel=None):
        hotel = hotel or self.hotel
        return self.client.get(reverse("hotel_detail", args=[hotel.pk]))

    def test_hotel_detail(self):
        response = self.get_detail_page()
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "hotel.html")

    def test_highlight_hotel(self):
        response = self.get_detail_page()
        self.assertContains(response, "Highlights")
        self.assertContains(response, self.highlight.name)
        self.assertContains(response, f'src="{self.highlight.icon.url}"')

    def test_highlight_without_icon_shows_only_the_name(self):
        hotel = HotelFactory(highlight=[HighlightFactory(name="Fietsverhuur", icon="")])
        response = self.get_detail_page(hotel)
        self.assertContains(response, "Fietsverhuur")
        self.assertNotContains(response, "/media/highlight_img/")

    def test_hotel_without_highlights_has_no_highlight_section(self):
        response = self.get_detail_page(HotelFactory())
        self.assertNotContains(response, "Highlights")

    def test_hotel_detail_shows_address_and_city(self):
        self.hotel.address = "Keizersgracht 117"
        self.hotel.save()
        response = self.get_detail_page()
        self.assertContains(response, ">Adres</dt>")
        self.assertContains(
            response, f">Keizersgracht 117, {self.hotel.city.name}</dd>"
        )

    def test_hotel_detail_without_address_shows_only_city(self):
        self.assertEqual(self.hotel.address, "")
        response = self.get_detail_page()
        self.assertContains(response, ">Adres</dt>")
        self.assertContains(response, f">{self.hotel.city.name}</dd>")

    def test_hotel_edit_page(self):
        # The old public edit page now forwards to the staff dashboard.
        response = self.client.get(reverse("hotel_edit", args=[self.hotel.id]))
        self.assertRedirects(
            response,
            reverse("dashboard:hotel_edit", args=[self.hotel.id]),
            fetch_redirect_response=False,
        )


class TestLogin(TestCase):
    password = "Landgraaf123"

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.staff = User.objects.create_user(
            username="Alex", password=cls.password, is_staff=True
        )
        cls.guest = User.objects.create_user(username="Sam", password=cls.password)
        cls.hotel = HotelFactory(
            highlight=[HighlightFactory(name="Free renting a bike")]
        )
        cls.old_edit_url = reverse("hotel_edit", args=[cls.hotel.pk])
        cls.edit_url = reverse("dashboard:hotel_edit", args=[cls.hotel.pk])

    def log_in(self, username, url=None):
        return self.client.post(
            url or reverse("login"), {"username": username, "password": self.password}
        )

    def test_login(self):
        response = self.client.get(reverse("login"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "login.html")

    def test_hotel_edit_page_redirects_to_login(self):
        # Anonymous users cannot reach the edit page: the old URL forwards to the
        # dashboard, which sends them to the login page and back again afterwards.
        response = self.client.get(self.old_edit_url, follow=True)
        self.assertRedirects(response, f"{reverse('login')}?next={self.edit_url}")

    def test_user_logged_in(self):
        self.client.force_login(self.staff)
        response = self.client.get(self.old_edit_url, follow=True)
        self.assertRedirects(response, self.edit_url)
        self.assertTemplateUsed(response, "dashboard/hotel_form.html")

    def test_logged_in_user_without_staff_status_gets_403(self):
        self.client.force_login(self.guest)
        response = self.client.get(self.edit_url)
        self.assertEqual(response.status_code, 403)

    def test_staff_goes_to_the_dashboard_after_logging_in(self):
        response = self.log_in("Alex")
        self.assertRedirects(response, reverse("dashboard:home"))

    def test_other_users_go_to_the_homepage_after_logging_in(self):
        response = self.log_in("Sam")
        self.assertRedirects(response, reverse("index"))

    def test_login_returns_to_the_next_page(self):
        response = self.log_in("Alex", f"{reverse('login')}?next={self.edit_url}")
        self.assertRedirects(response, self.edit_url)

    def test_wrong_password_stays_on_the_login_page(self):
        response = self.client.post(
            reverse("login"), {"username": "Alex", "password": "fout"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)


class EditPage(WebTest):
    def setUp(self):
        self.staff = get_user_model().objects.create_user(
            username="Alex", password="Landgraaf123", is_staff=True
        )
        self.city = CityFactory()
        self.highlight = HighlightFactory(name="Free renting a bike")
        self.hotel = HotelFactory(
            highlight=[self.highlight], name="Alex Hotel", price=100.00
        )
        self.room = RoomFactory(hotel=self.hotel, room_type="Double", price=100)

    def edit_form(self, page, field):
        # The dashboard pages also have the logout form in the header.
        return next(form for form in page.forms.values() if field in form.fields)

    def test_user_change_details(self):
        page = self.app.get(
            reverse("hotel_edit", args=[self.hotel.pk]), user=self.staff.username
        ).follow()
        form = self.edit_form(page, "name")
        form["name"] = "Hotel"
        form["address"] = "Keizersgracht 117"
        response = form.submit().follow()

        self.assertContains(response, "De wijzigingen in Hotel zijn opgeslagen.")
        self.hotel.refresh_from_db()
        self.assertEqual(self.hotel.name, "Hotel")
        self.assertEqual(self.hotel.address, "Keizersgracht 117")

    def test_user_change_room_price(self):
        # Prices are set per room; the site shows the lowest one as "vanaf" price.
        page = self.app.get(
            reverse("dashboard:room_edit", args=[self.room.pk]),
            user=self.staff.username,
        )
        form = self.edit_form(page, "price")
        form["price"] = "200"
        form.submit().follow()

        self.room.refresh_from_db()
        self.assertEqual(self.room.price, Decimal("200.00"))
        detail = self.app.get(reverse("hotel_detail", args=[self.hotel.pk]))
        self.assertIn("€ 200", detail.text)


class OrderPage(TestCase):
    def setUp(self):
        self.city = CityFactory()
        self.highlight = HighlightFactory(name="Free WiFi")
        self.hotel = HotelFactory(highlight=[self.highlight], city=self.city)
        self.room = RoomFactory(hotel=self.hotel, room_type="Single")
        self.order = OrderFactory(hotel=self.hotel, room=self.room)

    def test_order_page(self):
        # The checkout ("order") URL takes the hotel, not an existing order.
        response = self.client.get(reverse("order", args=[self.hotel.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "order.html")


class TestSearchFunctionality(TestCase):
    def setUp(self):
        self.city = CityFactory()
        self.highlight = HighlightFactory(name="Free WiFi")
        self.hotel = HotelFactory(
            highlight=[self.highlight], city=self.city, name="Alex Hotel"
        )
        self.room = RoomFactory(room_type="Single")

    def test_search_hotel(self):
        url = "{url}?{filter}={value}".format(
            url=reverse("stad", args=[self.city.name]),
            filter="q",
            value=self.hotel.name,
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.hotel.name)
