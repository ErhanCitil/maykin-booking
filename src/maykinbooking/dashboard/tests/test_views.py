import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format

from maykinbooking.base.models import City, Hotel, OrderStatus, Room, RoomBlock
from maykinbooking.base.tests.factories import OrderFactory, RoomBlockFactory

from ..forms import generate_city_id, next_hotel_id
from ..views import HotelDelete

# Smallest valid GIF, so Pillow accepts it as an image.
TINY_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04"
    b"\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)

TEMP_MEDIA = tempfile.mkdtemp()


def make_photo(name="photo.gif"):
    return SimpleUploadedFile(name, TINY_GIF, content_type="image/gif")


@override_settings(MEDIA_ROOT=TEMP_MEDIA)
class DashboardTestCase(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEMP_MEDIA, ignore_errors=True)

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.staff = User.objects.create_user(
            username="medewerker", password="secret", is_staff=True
        )
        cls.guest = User.objects.create_user(username="gast", password="secret")
        cls.amsterdam = City.objects.create(city_id="AMS", name="Amsterdam")
        cls.berlin = City.objects.create(city_id="BER", name="Berlijn")
        cls.hotel = Hotel.objects.create(
            city=cls.amsterdam, hotel_id="AMS01", name="Ibis Amsterdam Airport"
        )
        cls.room = Room.objects.create(
            hotel=cls.hotel,
            room_type="Double",
            price=Decimal("100.00"),
            description="Kamer voor twee",
        )
        Hotel.objects.create(city=cls.berlin, hotel_id="BER01", name="Hotel Adlon")

    def login_staff(self):
        self.client.force_login(self.staff)


class AccessTests(DashboardTestCase):
    def test_anonymous_user_is_sent_to_login(self):
        url = reverse("dashboard:home")
        response = self.client.get(url)
        self.assertRedirects(response, f"{reverse('login')}?next={url}")

    def test_non_staff_user_gets_403(self):
        self.client.force_login(self.guest)
        response = self.client.get(reverse("dashboard:home"))
        self.assertEqual(response.status_code, 403)

    def test_non_staff_user_cannot_change_hotels(self):
        self.client.force_login(self.guest)
        response = self.client.post(
            reverse("dashboard:hotel_delete", args=[self.hotel.pk])
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Hotel.objects.filter(pk=self.hotel.pk).exists())

    def test_staff_user_sees_dashboard(self):
        self.login_staff()
        response = self.client.get(reverse("dashboard:home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ibis Amsterdam Airport")
        self.assertContains(response, "Hotel Adlon")

    def test_header_links_to_dashboard_for_staff(self):
        self.login_staff()
        response = self.client.get(reverse("index"))
        self.assertContains(response, reverse("dashboard:home"))

    def test_staff_login_redirects_to_dashboard(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("login"))
        self.assertRedirects(response, reverse("dashboard:home"))

    def test_old_edit_urls_redirect_to_dashboard(self):
        response = self.client.get(reverse("hotels_edit"))
        self.assertRedirects(
            response, reverse("dashboard:home"), fetch_redirect_response=False
        )
        response = self.client.get(reverse("hotel_edit", args=[self.hotel.pk]))
        self.assertRedirects(
            response,
            reverse("dashboard:hotel_edit", args=[self.hotel.pk]),
            fetch_redirect_response=False,
        )


class HotelListTests(DashboardTestCase):
    def setUp(self):
        self.login_staff()

    def test_search_by_name(self):
        response = self.client.get(reverse("dashboard:home"), {"q": "adlon"})
        self.assertContains(response, "Hotel Adlon")
        self.assertNotContains(response, "Ibis Amsterdam Airport")

    def test_filter_by_city(self):
        response = self.client.get(
            reverse("dashboard:home"), {"city": self.amsterdam.pk}
        )
        self.assertContains(response, "Ibis Amsterdam Airport")
        self.assertNotContains(response, "Hotel Adlon")

    def test_shows_lowest_room_price(self):
        Room.objects.create(
            hotel=self.hotel,
            room_type="Single",
            price=Decimal("75.50"),
            description="x",
        )
        response = self.client.get(reverse("dashboard:home"))
        self.assertContains(response, "€ 75,50")


class HotelCrudTests(DashboardTestCase):
    def setUp(self):
        self.login_staff()

    def test_create_hotel(self):
        response = self.client.post(
            reverse("dashboard:hotel_create"),
            {
                "name": "Hotel V",
                "city": self.amsterdam.pk,
                "description": "Aan het Vondelpark",
                "is_available": "on",
                "upload": make_photo(),
                "terms": "<p>Inchecken vanaf 15:00</p>",
            },
        )
        hotel = Hotel.objects.get(name="Hotel V")
        self.assertRedirects(response, reverse("dashboard:hotel_edit", args=[hotel.pk]))
        self.assertEqual(hotel.hotel_id, "AMS02")
        self.assertTrue(hotel.upload.name.startswith("hotel_img/"))
        self.assertEqual(hotel.photo_url, hotel.upload.url)
        self.assertEqual(hotel.terms, "<p>Inchecken vanaf 15:00</p>")

    def test_create_hotel_with_address(self):
        response = self.client.post(
            reverse("dashboard:hotel_create"),
            {
                "name": "Hotel V",
                "address": "Weesperzijde 8",
                "city": self.amsterdam.pk,
                "is_available": "on",
            },
        )
        hotel = Hotel.objects.get(name="Hotel V")
        self.assertRedirects(response, reverse("dashboard:hotel_edit", args=[hotel.pk]))
        self.assertEqual(hotel.address, "Weesperzijde 8")

        response = self.client.get(reverse("hotel_detail", args=[hotel.pk]))
        self.assertContains(response, ">Weesperzijde 8, Amsterdam</dd>")

    def test_address_is_optional(self):
        self.client.post(
            reverse("dashboard:hotel_create"),
            {"name": "Hotel zonder adres", "city": self.amsterdam.pk},
        )
        self.assertEqual(Hotel.objects.get(name="Hotel zonder adres").address, "")

    def test_address_field_comes_right_after_the_name(self):
        response = self.client.get(reverse("dashboard:hotel_create"))
        form = response.context["form"]
        self.assertEqual(list(form.fields)[:2], ["name", "address"])
        self.assertEqual(form["address"].label, "Adres")
        self.assertContains(response, 'for="id_address"')
        self.assertContains(response, 'name="address"')

    def test_create_hotel_requires_city(self):
        response = self.client.post(
            reverse("dashboard:hotel_create"), {"name": "Zonder stad"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Hotel.objects.filter(name="Zonder stad").exists())

    def test_create_hotel_prefills_city_from_querystring(self):
        response = self.client.get(
            reverse("dashboard:hotel_create"), {"city": self.berlin.pk}
        )
        self.assertEqual(response.context["form"].initial["city"], self.berlin.pk)

    def test_edit_hotel(self):
        response = self.client.post(
            reverse("dashboard:hotel_edit", args=[self.hotel.pk]),
            {
                "name": "Ibis Amsterdam Centre",
                "city": self.berlin.pk,
                "description": "Nieuw",
            },
        )
        self.assertRedirects(
            response, reverse("dashboard:hotel_edit", args=[self.hotel.pk])
        )
        self.hotel.refresh_from_db()
        self.assertEqual(self.hotel.name, "Ibis Amsterdam Centre")
        self.assertEqual(self.hotel.city, self.berlin)
        self.assertFalse(self.hotel.is_available)
        # The hotel id stays the same when the city changes.
        self.assertEqual(self.hotel.hotel_id, "AMS01")

    def test_edit_hotel_address(self):
        url = reverse("dashboard:hotel_edit", args=[self.hotel.pk])
        data = {
            "name": self.hotel.name,
            "city": self.amsterdam.pk,
            "is_available": "on",
        }

        self.client.post(url, {**data, "address": "Schiphol Boulevard 701"})
        self.hotel.refresh_from_db()
        self.assertEqual(self.hotel.address, "Schiphol Boulevard 701")
        response = self.client.get(url)
        self.assertContains(response, 'value="Schiphol Boulevard 701"')

        # Clearing the address is allowed too.
        self.client.post(url, {**data, "address": ""})
        self.hotel.refresh_from_db()
        self.assertEqual(self.hotel.address, "")

    def test_address_longer_than_200_characters_is_rejected(self):
        response = self.client.post(
            reverse("dashboard:hotel_edit", args=[self.hotel.pk]),
            {"name": self.hotel.name, "city": self.amsterdam.pk, "address": "x" * 201},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("address", response.context["form"].errors)
        self.hotel.refresh_from_db()
        self.assertEqual(self.hotel.address, "")

    def test_remove_uploaded_photo(self):
        self.hotel.upload = make_photo()
        self.hotel.save()
        self.client.post(
            reverse("dashboard:hotel_edit", args=[self.hotel.pk]),
            {
                "name": self.hotel.name,
                "city": self.amsterdam.pk,
                "remove_upload": "on",
            },
        )
        self.hotel.refresh_from_db()
        self.assertFalse(self.hotel.upload)

    def test_delete_hotel_shows_consequences_and_deletes(self):
        # Orders protect their hotel (on_delete=PROTECT), so this hotel has none.
        url = reverse("dashboard:hotel_delete", args=[self.hotel.pk])
        response = self.client.get(url)
        self.assertContains(response, "1 kamer(s) worden ook verwijderd.")

        response = self.client.post(url)
        self.assertRedirects(response, reverse("dashboard:home"))
        self.assertFalse(Hotel.objects.filter(pk=self.hotel.pk).exists())
        self.assertFalse(Room.objects.filter(pk=self.room.pk).exists())


class RoomCrudTests(DashboardTestCase):
    def setUp(self):
        self.login_staff()

    def test_add_room(self):
        response = self.client.post(
            reverse("dashboard:room_create", args=[self.hotel.pk]),
            {
                "room_type": "Family",
                "price": "149.95",
                "inventory": "3",
                "description": "Ruimte voor vier",
                "is_available": "on",
                "upload": make_photo("room.gif"),
            },
        )
        self.assertRedirects(
            response, reverse("dashboard:hotel_edit", args=[self.hotel.pk])
        )
        room = self.hotel.room.get(room_type="Family")
        self.assertEqual(room.price, Decimal("149.95"))
        self.assertEqual(room.inventory, 3)
        self.assertTrue(room.upload.name.startswith("room_img/"))
        self.assertEqual(room.photo_url, room.upload.url)

    def test_change_room_price(self):
        self.client.post(
            reverse("dashboard:room_edit", args=[self.room.pk]),
            {
                "room_type": "Double",
                "price": "120",
                "inventory": "5",
                "description": self.room.description,
                "is_available": "on",
            },
        )
        self.room.refresh_from_db()
        self.assertEqual(self.room.price, Decimal("120.00"))

    def test_negative_price_is_rejected(self):
        response = self.client.post(
            reverse("dashboard:room_edit", args=[self.room.pk]),
            {"room_type": "Double", "price": "-5", "description": "x"},
        )
        self.assertEqual(response.status_code, 200)
        self.room.refresh_from_db()
        self.assertEqual(self.room.price, Decimal("100.00"))

    def test_delete_room(self):
        response = self.client.post(
            reverse("dashboard:room_delete", args=[self.room.pk])
        )
        self.assertRedirects(
            response, reverse("dashboard:hotel_edit", args=[self.hotel.pk])
        )
        self.assertFalse(Room.objects.filter(pk=self.room.pk).exists())

    def test_add_room_to_unknown_hotel_is_404(self):
        response = self.client.get(reverse("dashboard:room_create", args=[999999]))
        self.assertEqual(response.status_code, 404)

    def test_room_form_has_inventory_as_number_of_rooms(self):
        response = self.client.get(reverse("dashboard:room_edit", args=[self.room.pk]))
        form = response.context["form"]
        self.assertEqual(form["inventory"].label, "Aantal kamers")
        self.assertEqual(form["inventory"].value(), 5)
        self.assertContains(response, 'name="inventory"')

    def test_price_accepts_eight_digits(self):
        url = reverse("dashboard:room_edit", args=[self.room.pk])
        data = {"room_type": "Double", "inventory": "5", "description": "x"}

        self.client.post(url, {**data, "price": "999999.99"})
        self.room.refresh_from_db()
        self.assertEqual(self.room.price, Decimal("999999.99"))

        response = self.client.post(url, {**data, "price": "1000000.00"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("price", response.context["form"].errors)

    def test_inventory_above_999_is_rejected(self):
        response = self.client.post(
            reverse("dashboard:room_edit", args=[self.room.pk]),
            {"room_type": "Double", "price": "100", "inventory": "1000"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("inventory", response.context["form"].errors)


class InventoryWarningTests(DashboardTestCase):
    """US1.1: a lower inventory than the booked rooms gives a warning, and saves."""

    def setUp(self):
        self.login_staff()
        self.url = reverse("dashboard:room_edit", args=[self.room.pk])
        self.tomorrow = timezone.localdate() + timedelta(days=1)

    def book(self, start, nights=2, **kwargs):
        return OrderFactory(
            hotel=self.hotel,
            room=self.room,
            start_date=start,
            end_date=start + timedelta(days=nights),
            **kwargs,
        )

    def save_inventory(self, inventory):
        return self.client.post(
            self.url,
            {
                "room_type": "Double",
                "price": "100",
                "inventory": str(inventory),
                "description": "x",
                "is_available": "on",
            },
            follow=True,
        )

    def test_lowering_below_future_bookings_warns_and_saves(self):
        self.book(self.tomorrow)
        self.book(self.tomorrow + timedelta(days=1))

        response = self.save_inventory(1)

        self.room.refresh_from_db()
        self.assertEqual(self.room.inventory, 1)
        self.assertRedirects(
            response, reverse("dashboard:hotel_edit", args=[self.hotel.pk])
        )
        warnings = [m for m in response.context["messages"] if m.level_tag == "warning"]
        self.assertEqual(len(warnings), 1)
        # Only the night with two bookings is overbooked.
        self.assertIn("op 1 nacht zijn meer kamers bezet dan de 1", warnings[0].message)
        night = self.tomorrow + timedelta(days=1)
        self.assertIn(f"{date_format(night, 'j b Y')} (2)", warnings[0].message)
        self.assertContains(response, "Bestaande boekingen blijven staan.")

    def test_no_warning_when_the_bookings_fit(self):
        self.book(self.tomorrow)
        self.book(self.tomorrow)

        response = self.save_inventory(2)

        levels = [m.level_tag for m in response.context["messages"]]
        self.assertEqual(levels, ["success"])

    def test_past_and_cancelled_bookings_do_not_count(self):
        self.book(timezone.localdate() - timedelta(days=10))
        self.book(timezone.localdate() - timedelta(days=10))
        self.book(self.tomorrow, status=OrderStatus.CANCELLED)
        self.book(self.tomorrow)

        response = self.save_inventory(1)

        levels = [m.level_tag for m in response.context["messages"]]
        self.assertEqual(levels, ["success"])

    def test_inventory_zero_warns_for_every_booked_night(self):
        self.book(self.tomorrow, nights=12)

        response = self.save_inventory(0)

        self.room.refresh_from_db()
        self.assertEqual(self.room.inventory, 0)
        (warning,) = [
            m for m in response.context["messages"] if m.level_tag == "warning"
        ]
        self.assertIn("op 12 nachten", warning.message)
        # Long lists are cut short.
        self.assertIn("en nog 2 nachten", warning.message)


class ProtectedDeleteTests(DashboardTestCase):
    """Testpunt 10: a hotel or room with bookings cannot be deleted."""

    def setUp(self):
        self.login_staff()

    def test_hotel_with_bookings_has_no_delete_button(self):
        OrderFactory(hotel=self.hotel, room=self.room)
        OrderFactory(hotel=self.hotel, room=self.room, status=OrderStatus.CANCELLED)

        response = self.client.get(
            reverse("dashboard:hotel_delete", args=[self.hotel.pk])
        )

        self.assertContains(
            response,
            "Dit hotel heeft 2 boekingen en kan niet verwijderd worden. "
            "Zet het op niet beschikbaar.",
        )
        self.assertNotContains(response, "Ja, verwijderen")

    def test_posting_delete_for_hotel_with_bookings_redirects_with_message(self):
        OrderFactory(hotel=self.hotel, room=self.room)
        edit_url = reverse("dashboard:hotel_edit", args=[self.hotel.pk])

        response = self.client.post(
            reverse("dashboard:hotel_delete", args=[self.hotel.pk])
        )

        self.assertRedirects(response, edit_url)
        self.assertTrue(Hotel.objects.filter(pk=self.hotel.pk).exists())
        self.assertTrue(Room.objects.filter(pk=self.room.pk).exists())
        (message,) = get_messages(response.wsgi_request)
        self.assertEqual(message.level_tag, "error")
        self.assertEqual(
            message.message,
            "Dit hotel heeft 1 boeking en kan niet verwijderd worden. "
            "Zet het op niet beschikbaar.",
        )

    def test_room_with_bookings_cannot_be_deleted(self):
        OrderFactory(hotel=self.hotel, room=self.room)
        url = reverse("dashboard:room_delete", args=[self.room.pk])

        response = self.client.get(url)
        self.assertContains(
            response, "Deze kamer heeft 1 boeking en kan niet verwijderd worden."
        )
        self.assertNotContains(response, "Ja, verwijderen")

        response = self.client.post(url)
        self.assertRedirects(
            response, reverse("dashboard:room_edit", args=[self.room.pk])
        )
        self.assertTrue(Room.objects.filter(pk=self.room.pk).exists())

    def test_hotel_whose_other_room_has_bookings_is_protected(self):
        other_room = Room.objects.create(
            hotel=self.hotel, room_type="Single", price=Decimal("80"), description="x"
        )
        OrderFactory(hotel=self.hotel, room=other_room)

        response = self.client.post(
            reverse("dashboard:hotel_delete", args=[self.hotel.pk])
        )

        self.assertRedirects(
            response, reverse("dashboard:hotel_edit", args=[self.hotel.pk])
        )
        self.assertTrue(Hotel.objects.filter(pk=self.hotel.pk).exists())

    def test_a_booking_made_meanwhile_gives_a_message_instead_of_a_500(self):
        OrderFactory(hotel=self.hotel, room=self.room)
        # The check says "no bookings", but one comes in before the delete:
        # the ProtectedError of the database is caught.
        with mock.patch.object(
            HotelDelete,
            "get_protected_message",
            autospec=True,
            side_effect=[None, "Dit hotel heeft nu een boeking."],
        ):
            response = self.client.post(
                reverse("dashboard:hotel_delete", args=[self.hotel.pk])
            )

        self.assertRedirects(
            response, reverse("dashboard:hotel_edit", args=[self.hotel.pk])
        )
        self.assertTrue(Hotel.objects.filter(pk=self.hotel.pk).exists())
        (message,) = get_messages(response.wsgi_request)
        self.assertEqual(message.message, "Dit hotel heeft nu een boeking.")

    def test_protected_error_without_a_message_gets_a_general_one(self):
        OrderFactory(hotel=self.hotel, room=self.room)
        with mock.patch.object(
            HotelDelete, "get_protected_message", autospec=True, return_value=None
        ):
            response = self.client.post(
                reverse("dashboard:hotel_delete", args=[self.hotel.pk])
            )

        self.assertRedirects(
            response, reverse("dashboard:hotel_edit", args=[self.hotel.pk])
        )
        (message,) = get_messages(response.wsgi_request)
        self.assertEqual(
            message.message,
            "Ibis Amsterdam Airport heeft boekingen en kan niet verwijderd worden.",
        )

    def test_the_delete_is_handled_in_post(self):
        # Django 4 deletes in form_valid() instead of delete(): the handling of
        # bookings must not depend on delete() being called.
        from ..views import DeleteWithMessageMixin

        self.assertIn("post", vars(DeleteWithMessageMixin))
        self.assertNotIn("delete", vars(DeleteWithMessageMixin))

    def test_room_without_bookings_is_deleted_with_its_blocks(self):
        RoomBlockFactory(room=self.room)
        url = reverse("dashboard:room_delete", args=[self.room.pk])

        response = self.client.get(url)
        self.assertContains(response, "1 blokkade(s) van deze kamer worden ook")

        response = self.client.post(url)
        self.assertRedirects(
            response, reverse("dashboard:hotel_edit", args=[self.hotel.pk])
        )
        self.assertFalse(Room.objects.filter(pk=self.room.pk).exists())
        self.assertFalse(RoomBlock.objects.exists())

    def test_consequences_no_longer_mention_deleting_bookings(self):
        response = self.client.get(
            reverse("dashboard:hotel_delete", args=[self.hotel.pk])
        )
        self.assertNotContains(response, "boeking(en) worden ook verwijderd")
        response = self.client.get(
            reverse("dashboard:hotel_edit", args=[self.hotel.pk])
        )
        self.assertNotContains(response, "kamers en boekingen van dit hotel")


class DashboardHomeBookingsTileTests(DashboardTestCase):
    def setUp(self):
        self.login_staff()

    def test_tile_counts_confirmed_arrivals_today_and_upcoming(self):
        today = timezone.localdate()
        for start, status in (
            (today, OrderStatus.CONFIRMED),
            (today, OrderStatus.CONFIRMED),
            (today, OrderStatus.CANCELLED),
            (today + timedelta(days=3), OrderStatus.CONFIRMED),
            (today - timedelta(days=3), OrderStatus.CONFIRMED),
        ):
            OrderFactory(
                hotel=self.hotel,
                room=self.room,
                start_date=start,
                end_date=start + timedelta(days=1),
                status=status,
            )

        response = self.client.get(reverse("dashboard:home"))

        self.assertEqual(response.context["arrivals_today"], 2)
        self.assertEqual(response.context["upcoming_arrivals"], 3)
        self.assertContains(response, "3 komende aankomsten")
        list_url = reverse("dashboard:booking_list")
        self.assertContains(
            response, f"{list_url}?snel=aankomst_vandaag&amp;status=confirmed"
        )

    def test_subnav_links_to_bookings(self):
        response = self.client.get(reverse("dashboard:home"))
        self.assertContains(
            response, f'href="{reverse("dashboard:booking_list")}"', count=1
        )


class CityTests(DashboardTestCase):
    def setUp(self):
        self.login_staff()

    def test_create_city(self):
        response = self.client.post(
            reverse("dashboard:city_create"), {"name": "Rotterdam"}
        )
        city = City.objects.get(name="Rotterdam")
        self.assertEqual(city.city_id, "ROT")
        self.assertRedirects(
            response, f"{reverse('dashboard:hotel_create')}?city={city.pk}"
        )

    def test_duplicate_city_is_rejected(self):
        response = self.client.post(
            reverse("dashboard:city_create"), {"name": "amsterdam"}
        )
        self.assertContains(response, "Deze stad bestaat al.")
        self.assertEqual(City.objects.filter(name__iexact="amsterdam").count(), 1)


class IdGenerationTests(DashboardTestCase):
    def test_next_hotel_id(self):
        self.assertEqual(next_hotel_id(self.amsterdam), "AMS02")
        self.assertEqual(next_hotel_id(self.berlin), "BER02")

    def test_generate_city_id_is_unique_and_ascii(self):
        self.assertEqual(generate_city_id("Amstelveen"), "AMS2")
        self.assertEqual(generate_city_id("Zürich"), "ZUR")


class PublicHotelPageTests(DashboardTestCase):
    def test_public_page_has_no_upload_form(self):
        response = self.client.get(reverse("hotel_detail", args=[self.hotel.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'type="file"')

    def test_anonymous_users_cannot_upload_photos(self):
        response = self.client.post(
            reverse("hotel_detail", args=[self.hotel.pk]), {"upload": make_photo()}
        )
        self.assertEqual(response.status_code, 405)
