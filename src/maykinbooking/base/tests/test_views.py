from datetime import timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from django_webtest import WebTest

from .factories import (
    CityFactory,
    HighlightFactory,
    HotelFactory,
    OrderFactory,
    RoomBlockFactory,
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


class PeriodTestMixin:
    def setUp(self):
        super().setUp()
        self.start = timezone.localdate() + timedelta(days=14)
        self.end = self.start + timedelta(days=2)
        self.period = {"van": self.start.isoformat(), "tot": self.end.isoformat()}

    def make_hotel(self, city, name, *, full=False, **kwargs):
        """A bookable hotel with one room; ``full``: booked for the period."""
        kwargs.setdefault("is_available", True)
        hotel = HotelFactory(city=city, name=name, **kwargs)
        room = RoomFactory(hotel=hotel, is_available=True, inventory=1)
        if full:
            OrderFactory(
                hotel=hotel, room=room, start_date=self.start, end_date=self.end
            )
        return hotel


class StadAvailabilityFilterTests(PeriodTestMixin, TestCase):
    """US1.3 and testpunt 13: only hotels with a free room for the period."""

    def setUp(self):
        super().setUp()
        self.city = CityFactory(name="Amsterdam")
        self.free = self.make_hotel(self.city, "Free Hotel")
        self.full = self.make_hotel(self.city, "Full Hotel", full=True)
        self.closed = self.make_hotel(self.city, "Closed Hotel", is_available=False)
        self.url = reverse("stad", args=[self.city.name])

    def get(self, **params):
        return self.client.get(self.url, params)

    def test_filter_form_is_shown(self):
        response = self.get()
        self.assertContains(response, 'name="van"')
        self.assertContains(response, 'name="tot"')
        self.assertContains(response, 'type="date"')
        self.assertContains(response, "Toon beschikbaar")

    def test_without_period_all_hotels_are_listed(self):
        response = self.get()
        self.assertEqual(
            list(response.context["object_list"]), [self.closed, self.free, self.full]
        )

    def test_period_lists_only_hotels_with_a_free_room(self):
        response = self.get(**self.period)
        self.assertEqual(list(response.context["object_list"]), [self.free])
        self.assertContains(response, "1 hotel ")
        self.assertNotContains(response, "Full Hotel")
        # The form keeps the dates.
        self.assertContains(response, f'value="{self.start.isoformat()}"')

    def test_blocked_hotel_is_not_listed(self):
        RoomBlockFactory(
            room=self.free.room.get(), start_date=self.start, end_date=self.end
        )
        response = self.get(**self.period)
        self.assertEqual(list(response.context["object_list"]), [])
        self.assertContains(response, "Geen hotels met een vrije kamer")

    def test_period_and_search_together(self):
        self.make_hotel(self.city, "Another Free Hotel")
        response = self.get(q="another", **self.period)
        self.assertEqual(
            [hotel.name for hotel in response.context["object_list"]],
            ["Another Free Hotel"],
        )

    def test_hotel_and_book_links_carry_the_period(self):
        response = self.get(**self.period)
        query = escape(urlencode(self.period))
        hotel_url = reverse("hotel_detail", args=[self.free.pk])
        order_url = reverse("order", args=[self.free.pk])
        self.assertContains(response, f'href="{hotel_url}?{query}"')
        self.assertContains(response, f'href="{order_url}?{query}"')

    def test_without_period_links_have_no_dates(self):
        response = self.get()
        self.assertContains(
            response, f'href="{reverse("hotel_detail", args=[self.free.pk])}"'
        )
        self.assertNotContains(response, reverse("order", args=[self.free.pk]))

    def test_invalid_period_shows_an_error_and_does_not_filter(self):
        today = timezone.localdate()
        cases = {
            "past": {
                "van": (today - timedelta(days=1)).isoformat(),
                "tot": (today + timedelta(days=1)).isoformat(),
            },
            "end before start": {"van": self.end.isoformat(), "tot": self.start},
            "same day": {"van": self.start.isoformat(), "tot": self.start},
            "too long": {
                "van": self.start.isoformat(),
                "tot": (self.start + timedelta(days=31)).isoformat(),
            },
            "only arrival": {"van": self.start.isoformat(), "tot": ""},
            "unreadable": {"van": "morgen", "tot": "overmorgen"},
        }
        for name, params in cases.items():
            with self.subTest(name):
                response = self.get(**params)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'role="alert"')
                self.assertContains(response, "Deze periode klopt niet")
                self.assertEqual(len(response.context["object_list"]), 3)
                self.assertNotContains(response, reverse("order", args=[self.free.pk]))

    def test_error_messages_are_specific(self):
        response = self.get(van=self.end.isoformat(), tot=self.start.isoformat())
        self.assertContains(response, "Uitchecken moet na de incheckdatum zijn.")
        response = self.get(van=self.start.isoformat())
        self.assertContains(
            response, "Vul zowel een incheck- als een uitcheckdatum in."
        )

    def test_empty_dates_are_no_filter(self):
        response = self.get(q="", van="", tot="")
        self.assertNotContains(response, 'role="alert"')
        self.assertEqual(len(response.context["object_list"]), 3)


class StadPaginationTests(PeriodTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.city = CityFactory(name="Utrecht")
        for number in range(13):
            self.make_hotel(self.city, f"Hotel {number:02d}")
        self.make_hotel(self.city, "Hotel Vol", full=True)
        self.make_hotel(self.city, "Other")
        self.url = reverse("stad", args=[self.city.name])

    def test_search_and_period_are_kept_when_paginating(self):
        params = {"q": "hotel", **self.period}
        response = self.client.get(self.url, params)
        self.assertEqual(response.context["paginator"].count, 13)
        self.assertContains(response, "13 hotels")
        next_link = escape(f"?page=2&{urlencode(params)}")
        self.assertContains(response, f'href="{next_link}"')

        response = self.client.get(self.url, {"page": 2, **params})
        self.assertEqual(
            [hotel.name for hotel in response.context["object_list"]], ["Hotel 12"]
        )
        previous_link = escape(f"?page=1&{urlencode(params)}")
        self.assertContains(response, f'href="{previous_link}"')

    def test_invalid_values_are_not_kept_when_paginating(self):
        response = self.client.get(self.url, {"van": "morgen", "tot": ""})
        self.assertContains(response, 'href="?page=2"')


class StadQueryCountTests(PeriodTestMixin, TestCase):
    """The availability filter costs the same number of queries for any city."""

    def count_queries(self, city_name, number_of_hotels):
        city = CityFactory(name=city_name)
        for number in range(number_of_hotels):
            self.make_hotel(city, f"Hotel {number}", full=number % 2 == 0)
        url = reverse("stad", args=[city.name])
        self.client.get(url, self.period)  # warm up (session, content types)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(url, self.period)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["object_list"]), number_of_hotels // 2)
        return len(queries)

    def test_same_number_of_queries_for_2_and_10_hotels(self):
        self.assertEqual(
            self.count_queries("Delft", 2), self.count_queries("Gouda", 10)
        )


class HotelDetailPeriodTests(PeriodTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.hotel = HotelFactory(is_available=True)
        self.single = RoomFactory(
            hotel=self.hotel, room_type="Single", price=80, inventory=1
        )
        self.double = RoomFactory(
            hotel=self.hotel, room_type="Double", price=100, inventory=1
        )
        self.url = reverse("hotel_detail", args=[self.hotel.pk])
        self.order_url = reverse("order", args=[self.hotel.pk])

    def book(self, room):
        OrderFactory(
            hotel=self.hotel, room=room, start_date=self.start, end_date=self.end
        )

    def test_book_links_carry_the_period(self):
        response = self.client.get(self.url, self.period)
        query = escape(urlencode(self.period))
        # "Boek nu" and one "Boek" link per room.
        self.assertContains(response, f'href="{self.order_url}?{query}"', count=3)
        self.assertContains(
            response,
            f'href="{reverse("stad", args=[self.hotel.city.name])}?{query}"',
        )

    def test_without_period_book_links_have_no_dates(self):
        response = self.client.get(self.url)
        self.assertContains(response, f'href="{self.order_url}"', count=3)

    def test_invalid_period_is_ignored(self):
        response = self.client.get(self.url, {"van": "morgen", "tot": "x"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'href="{self.order_url}"', count=3)

    def test_room_that_is_not_free_has_no_book_link(self):
        self.book(self.single)
        response = self.client.get(self.url, self.period)
        self.assertContains(response, "Niet vrij in deze periode", count=1)
        self.assertContains(response, f'href="{self.order_url}?', count=2)
        self.assertNotContains(response, "Geen kamers vrij in deze periode")

    def test_no_free_rooms_is_announced(self):
        self.book(self.single)
        self.book(self.double)
        response = self.client.get(self.url, self.period)
        self.assertContains(response, "Geen kamers vrij in deze periode")
        self.assertContains(response, "Niet vrij in deze periode", count=2)
