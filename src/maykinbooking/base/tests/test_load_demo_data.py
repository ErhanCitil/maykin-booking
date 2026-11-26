"""
Tests for the ``load_demo_data`` management command and the demo dataset in
``base/demo_data/hotels.json``.

The command deletes all cities, hotels, rooms, orders and reviews, so these tests
only ever run it against the Django test database.
"""
import json
import re
import shutil
import tempfile
from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import CommandError, call_command
from django.db.models import Count, Min, Q
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils.html import escape

from maykinbooking.dashboard.forms import next_hotel_id

from ..management.commands import load_demo_data
from ..models import (
    ROOM_CHOICES,
    City,
    EmailLog,
    Highlight,
    Hotel,
    Order,
    OrderNight,
    OrderStatusChange,
    Review,
    Room,
    RoomBlock,
)

STATIC_DIR = Path(__file__).resolve().parents[2] / "static"

DEMO_CITIES = {
    "AMS": "Amsterdam",
    "ANT": "Antwerpen",
    "ATH": "Athene",
    "BAK": "Bangkok",
    "BAR": "Barcelona",
    "BER": "Berlijn",
}
DEMO_HOTEL_COUNT = 201
DEMO_ROOM_COUNT = 510

OSM_CREDIT = re.compile(
    r'Hoteldata © <a href="https://www\.openstreetmap\.org/copyright"[^>]*>'
    r"OpenStreetMap-bijdragers</a>"
)


def run_command(*args, **options):
    """Run ``load_demo_data`` and return what it wrote to stdout."""
    stdout = StringIO()
    call_command("load_demo_data", *args, stdout=stdout, stderr=StringIO(), **options)
    return stdout.getvalue()


def demo_snapshot():
    """Everything the command creates, without primary keys."""
    hotels = (
        Hotel.objects.select_related("city")
        .prefetch_related("room")
        .order_by("hotel_id")
    )
    return [
        (
            hotel.city.city_id,
            hotel.city.name,
            hotel.hotel_id,
            hotel.name,
            hotel.address,
            hotel.description,
            hotel.terms,
            hotel.image.name,
            hotel.price,
            hotel.is_available,
            sorted(
                (room.room_type, room.price, room.description, room.image.name)
                for room in hotel.room.all()
            ),
        )
        for hotel in hotels
    ]


def make_room(**fields):
    room = {
        "room_type": "Double",
        "price": 120,
        "description": "Tweepersoonskamer met uitzicht op de gracht.",
        "image": "demo/rooms/double-1.jpg",
    }
    room.update(fields)
    return room


def make_hotel(**fields):
    hotel = {
        "name": "Testhotel",
        "street": "Keizersgracht",
        "housenumber": "117",
        "description": "Een hotel om mee te testen.",
        "terms": "<p>Inchecken vanaf 15:00 uur.</p>",
        "image": "demo/hotels/hotel-01.jpg",
        "rooms": [make_room()],
    }
    hotel.update(fields)
    return hotel


def make_city(**fields):
    city = {"city_id": "TST", "name": "Teststad", "hotels": [make_hotel()]}
    city.update(fields)
    return city


def make_dataset(*cities):
    return {"cities": list(cities) or [make_city()]}


class ExistingDataMixin:
    """Data that is already in the database before the command runs."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="beheerder", email="beheer@example.com", password="x-Geheim-123"
        )
        cls.highlight = Highlight.objects.create(name="Gratis fietsverhuur")
        city = City.objects.create(city_id="OLD", name="Oudstad")
        hotel = Hotel.objects.create(
            city=city, hotel_id="OLD01", name="Onzinhotel", price=Decimal("50")
        )
        hotel.highlight.add(cls.highlight)
        room = Room.objects.create(
            hotel=hotel,
            room_type="Single",
            price=Decimal("50"),
            description="Oude kamer",
            image="oud.jpg",
        )
        cls.order = Order.objects.create(
            hotel=hotel,
            room=room,
            first_name="Alex",
            last_name="de Landgraaf",
            email="alex@example.com",
            address="Keizersgracht 117",
            zipcode="1015CJ",
            start_date=date(2027, 1, 1),
            end_date=date(2027, 1, 3),
            total_price=Decimal("100"),
        )
        cls.review = Review.objects.create(
            hotel=hotel, name="Sam", rating=4, description="Prima verblijf"
        )

    def assertExistingDataUntouched(self):
        self.assertEqual(list(City.objects.values_list("city_id", flat=True)), ["OLD"])
        self.assertEqual(
            list(Hotel.objects.values_list("name", flat=True)), ["Onzinhotel"]
        )
        self.assertEqual(Room.objects.count(), 1)
        self.assertEqual(
            list(Order.objects.values_list("pk", flat=True)), [self.order.pk]
        )
        self.assertEqual(
            list(Review.objects.values_list("pk", flat=True)), [self.review.pk]
        )

    def assertDemoDataLoaded(self):
        self.assertEqual(City.objects.count(), len(DEMO_CITIES))
        self.assertEqual(Hotel.objects.count(), DEMO_HOTEL_COUNT)
        self.assertEqual(Room.objects.count(), DEMO_ROOM_COUNT)


class LoadDemoDataTests(ExistingDataMixin, TestCase):
    def test_noinput_loads_all_cities_hotels_and_rooms(self):
        run_command("--noinput")
        self.assertDemoDataLoaded()
        self.assertEqual(dict(City.objects.values_list("city_id", "name")), DEMO_CITIES)

    def test_no_input_alias_loads_all_cities_hotels_and_rooms(self):
        run_command("--no-input")
        self.assertDemoDataLoaded()

    def test_second_run_gives_exactly_the_same_data(self):
        run_command("--noinput")
        first_run = demo_snapshot()

        run_command("--noinput")

        self.assertDemoDataLoaded()
        self.assertEqual(demo_snapshot(), first_run)

    def test_old_cities_hotels_orders_and_reviews_are_deleted(self):
        run_command("--noinput")
        self.assertFalse(City.objects.filter(city_id="OLD").exists())
        self.assertFalse(Hotel.objects.filter(name="Onzinhotel").exists())
        self.assertFalse(Order.objects.exists())
        self.assertFalse(Review.objects.exists())

    def test_users_and_highlights_are_kept(self):
        run_command("--noinput")
        user = get_user_model().objects.get(pk=self.user.pk)
        self.assertTrue(user.check_password("x-Geheim-123"))
        self.assertTrue(Highlight.objects.filter(pk=self.highlight.pk).exists())

    def test_output_reports_what_was_deleted_and_created(self):
        output = run_command("--noinput")
        self.assertIn(
            "Verwijderd: 1 boekingen, 1 reviews, 1 kamers, 1 hotels, 1 steden.", output
        )
        self.assertIn("Demodata geladen: 6 steden, 201 hotels, 510 kamers.", output)

    def test_verbosity_zero_prints_nothing(self):
        output = run_command("--noinput", verbosity=0)
        self.assertEqual(output, "")
        self.assertDemoDataLoaded()

    def test_error_halfway_through_creating_rolls_everything_back(self):
        original_bulk_create = Room.objects.bulk_create
        calls = []

        def fail_on_third_hotel(*args, **kwargs):
            calls.append(1)
            if len(calls) == 3:
                raise RuntimeError("database weg")
            return original_bulk_create(*args, **kwargs)

        with mock.patch.object(
            Room.objects, "bulk_create", side_effect=fail_on_third_hotel
        ):
            with self.assertRaises(RuntimeError):
                run_command("--noinput")

        self.assertEqual(len(calls), 3)
        self.assertExistingDataUntouched()

    @override_settings(ENVIRONMENT="production")
    def test_refuses_to_run_in_production(self):
        with self.assertRaisesMessage(CommandError, "niet bedoeld voor productie"):
            run_command("--noinput")
        self.assertExistingDataUntouched()

    def test_every_room_type_gets_inventory_5(self):
        run_command("--noinput")
        self.assertEqual(
            set(Room.objects.values_list("inventory", flat=True)),
            {load_demo_data.ROOM_INVENTORY},
        )
        self.assertEqual(load_demo_data.ROOM_INVENTORY, 5)

    def test_orders_with_nights_history_mails_and_room_blocks_are_deleted(self):
        # Orders protect their room and hotel, so they have to go first.
        room = Room.objects.get()
        OrderNight.objects.create(
            order=self.order, date=date(2027, 1, 1), price=Decimal("50")
        )
        OrderStatusChange.objects.create(
            order=self.order, field="status", new_value="confirmed", actor_type="system"
        )
        EmailLog.objects.create(
            order=self.order, kind="booking_confirmation", recipient="a@example.com"
        )
        RoomBlock.objects.create(
            room=room, start_date=date(2027, 2, 1), end_date=date(2027, 2, 3)
        )

        output = run_command("--noinput")

        self.assertIn("Verwijderd: 1 boekingen, 1 reviews, 1 kamers", output)
        self.assertDemoDataLoaded()
        for model in [Order, OrderNight, OrderStatusChange, EmailLog, RoomBlock]:
            with self.subTest(model=model.__name__):
                self.assertFalse(model.objects.exists())

    def test_new_dashboard_hotel_continues_the_hotel_id_sequence(self):
        run_command("--noinput")
        amsterdam = City.objects.get(city_id="AMS")
        count = amsterdam.test.count()
        self.assertEqual(next_hotel_id(amsterdam), f"AMS{count + 1:02d}")


class LoadDemoDataPromptTests(ExistingDataMixin, TestCase):
    def test_answer_nee_cancels_without_changes(self):
        with mock.patch("builtins.input", return_value="nee") as prompt:
            output = run_command()
        prompt.assert_called_once()
        self.assertIn("Geannuleerd, er is niets gewijzigd.", output)
        self.assertExistingDataUntouched()

    def test_any_answer_other_than_ja_or_yes_cancels(self):
        for answer in ["", "n", "no", "y", "j", "jaa", "ja graag", "ok"]:
            with self.subTest(answer=answer):
                with mock.patch("builtins.input", return_value=answer):
                    output = run_command()
                self.assertIn("Geannuleerd", output)
                self.assertExistingDataUntouched()

    def test_closed_stdin_changes_nothing(self):
        with mock.patch("builtins.input", side_effect=EOFError):
            with self.assertRaises(EOFError):
                run_command()
        self.assertExistingDataUntouched()

    def test_answer_ja_loads_demo_data(self):
        with mock.patch("builtins.input", return_value="ja") as prompt:
            run_command()
        prompt.assert_called_once()
        self.assertDemoDataLoaded()
        self.assertFalse(Order.objects.exists())

    def test_answer_yes_in_capitals_with_whitespace_loads_demo_data(self):
        with mock.patch("builtins.input", return_value="  YES \n"):
            run_command()
        self.assertDemoDataLoaded()

    def test_prompt_says_what_will_be_deleted(self):
        with mock.patch("builtins.input", return_value="nee") as prompt:
            run_command()
        (question,), _kwargs = prompt.call_args
        self.assertIn("hotels.json", question)
        self.assertIn("boekingen en reviews", question)
        self.assertIn("Gebruikers en accounts blijven bestaan", question)
        self.assertIn("'ja'", question)

    def test_noinput_does_not_ask_for_confirmation(self):
        with mock.patch("builtins.input") as prompt:
            run_command("--noinput")
        prompt.assert_not_called()
        self.assertDemoDataLoaded()


class LoadDemoDataValidationTests(ExistingDataMixin, TestCase):
    """The command checks the whole file first and changes nothing if it is wrong."""

    def setUp(self):
        tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp_dir)
        self.data_file = Path(tmp_dir) / "hotels.json"
        patcher = mock.patch.object(load_demo_data, "DATA_FILE", self.data_file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_dataset(self, data):
        self.data_file.write_text(json.dumps(data), encoding="utf-8")

    def assertRejected(self, data, message=""):
        self.write_dataset(data)
        with self.assertRaisesMessage(CommandError, message):
            run_command("--noinput")
        self.assertExistingDataUntouched()

    def test_valid_small_dataset_is_loaded(self):
        self.write_dataset(
            make_dataset(
                make_city(
                    hotels=[
                        make_hotel(
                            name="Grachtenhotel",
                            rooms=[
                                make_room(room_type="Family", price=180),
                                make_room(room_type="Single", price="75.5"),
                            ],
                        )
                    ]
                )
            )
        )

        output = run_command("--noinput")

        self.assertIn("Demodata geladen: 1 steden, 1 hotels, 2 kamers.", output)
        hotel = Hotel.objects.select_related("city").get()
        self.assertEqual(hotel.city.city_id, "TST")
        self.assertEqual(hotel.hotel_id, "TST01")
        self.assertEqual(hotel.name, "Grachtenhotel")
        self.assertEqual(hotel.address, "Keizersgracht 117")
        self.assertEqual(hotel.price, Decimal("75.50"))
        self.assertTrue(hotel.is_available)
        self.assertEqual(hotel.image.name, "demo/hotels/hotel-01.jpg")
        self.assertEqual(
            sorted(hotel.room.values_list("room_type", "price", "is_available")),
            [("Family", Decimal("180.00"), True), ("Single", Decimal("75.50"), True)],
        )
        self.assertFalse(Order.objects.exists())

    def test_loaded_prices_render_in_dutch_format_on_checkout(self):
        self.write_dataset(
            make_dataset(
                make_city(hotels=[make_hotel(rooms=[make_room(price="75.50")])])
            )
        )
        run_command("--noinput")
        hotel = Hotel.objects.get()
        room = hotel.room.get()
        url = reverse("order", args=[hotel.pk])
        start = date.today() + timedelta(days=7)

        response = self.client.get(url)
        self.assertContains(response, "€ 75,50")

        response = self.client.post(
            url,
            {
                "checkout-current_step": "stay",
                "stay-start_date": start,
                "stay-end_date": start + timedelta(days=2),
                "stay-room": room.pk,
            },
        )
        self.assertContains(response, "€ 75,50 × 2 nachten")
        self.assertContains(response, "€ 151,00")

    def test_missing_file(self):
        with self.assertRaisesMessage(CommandError, "niet lezen"):
            run_command("--noinput")
        self.assertExistingDataUntouched()

    def test_invalid_json(self):
        self.data_file.write_text('{"cities": [', encoding="utf-8")
        with self.assertRaisesMessage(CommandError, "niet lezen"):
            run_command("--noinput")
        self.assertExistingDataUntouched()

    def test_file_without_cities(self):
        for data in [{}, {"cities": []}, {"cities": None}, [], "Amsterdam"]:
            with self.subTest(data=data):
                self.assertRejected(data, "bevat geen steden")

    def test_missing_or_empty_required_fields(self):
        cases = [
            ("city_id", make_city(city_id="")),
            ("name", make_city(name="   ")),
            ("name", make_city(hotels=[make_hotel(name=None)])),
            ("description", make_city(hotels=[make_hotel(description="")])),
            ("terms", make_city(hotels=[make_hotel(terms=None)])),
            ("image", make_city(hotels=[make_hotel(image="")])),
            (
                "description",
                make_city(hotels=[make_hotel(rooms=[make_room(description="")])]),
            ),
            ("image", make_city(hotels=[make_hotel(rooms=[make_room(image=None)])])),
        ]
        for field, city in cases:
            with self.subTest(field=field, city=city):
                self.assertRejected(
                    make_dataset(city), f"'{field}' ontbreekt of is leeg"
                )

    def test_missing_keys(self):
        city_without_name = make_city()
        del city_without_name["name"]
        hotel_without_terms = make_hotel()
        del hotel_without_terms["terms"]
        for city in [city_without_name, make_city(hotels=[hotel_without_terms])]:
            with self.subTest(city=city):
                self.assertRejected(make_dataset(city), "ontbreekt of is leeg")

    def test_values_longer_than_the_database_column(self):
        too_long = "x" * 101
        cases = [
            make_city(city_id=too_long),
            make_city(name=too_long),
            make_city(hotels=[make_hotel(name=too_long)]),
            make_city(hotels=[make_hotel(image=f"demo/{too_long}.jpg")]),
            make_city(hotels=[make_hotel(rooms=[make_room(image=too_long)])]),
        ]
        for city in cases:
            with self.subTest(city=city):
                self.assertRejected(make_dataset(city), "langer dan 100 tekens")

    def test_address_is_street_and_housenumber(self):
        without_housenumber = make_hotel()
        del without_housenumber["housenumber"]
        without_street_or_housenumber = make_hotel()
        del without_street_or_housenumber["street"]
        del without_street_or_housenumber["housenumber"]
        cases = [
            (make_hotel(street="Damrak", housenumber="1-5"), "Damrak 1-5"),
            (without_housenumber, "Keizersgracht"),
            (make_hotel(housenumber=""), "Keizersgracht"),
            (make_hotel(housenumber=None), "Keizersgracht"),
            (make_hotel(housenumber="   "), "Keizersgracht"),
            (make_hotel(street=" Damrak ", housenumber=" 1a "), "Damrak 1a"),
            (without_street_or_housenumber, ""),
        ]
        for hotel, address in cases:
            with self.subTest(hotel=hotel):
                self.write_dataset(make_dataset(make_city(hotels=[hotel])))
                run_command("--noinput")
                self.assertEqual(Hotel.objects.get().address, address)

    def test_address_of_exactly_200_characters_is_allowed(self):
        hotel = make_hotel(street="S" * 195, housenumber="1234")
        self.write_dataset(make_dataset(make_city(hotels=[hotel])))
        run_command("--noinput")
        self.assertEqual(len(Hotel.objects.get().address), 200)

    def test_address_longer_than_the_database_column(self):
        hotel = make_hotel(street="S" * 196, housenumber="1234")
        self.assertRejected(
            make_dataset(make_city(hotels=[hotel])), "langer dan 200 tekens"
        )

    def test_street_or_housenumber_that_is_not_text(self):
        for hotel in [make_hotel(housenumber=117), make_hotel(street=["Damrak"])]:
            with self.subTest(hotel=hotel):
                self.assertRejected(
                    make_dataset(make_city(hotels=[hotel])), "moet tekst zijn"
                )

    def test_name_of_exactly_100_characters_is_allowed(self):
        name = "H" * 100
        self.write_dataset(make_dataset(make_city(hotels=[make_hotel(name=name)])))
        run_command("--noinput")
        self.assertEqual(Hotel.objects.get().name, name)

    def test_unknown_room_type(self):
        for room_type in ["Suite", "double", "", None]:
            with self.subTest(room_type=room_type):
                room = make_room(room_type=room_type)
                city = make_city(hotels=[make_hotel(rooms=[room])])
                self.assertRejected(make_dataset(city), "onbekend kamertype")

    def test_prices_outside_the_allowed_range(self):
        for price in [
            0,
            -10,
            "0.00",
            10000,
            "9999.995",
            "NaN",
            "Infinity",
            "-Infinity",
        ]:
            with self.subTest(price=price):
                room = make_room(price=price)
                city = make_city(hotels=[make_hotel(rooms=[room])])
                self.assertRejected(make_dataset(city), "valt buiten het bereik")

    def test_prices_that_are_not_numbers(self):
        for price in ["abc", "75,50", "", None, True, [75], {"bedrag": 75}]:
            with self.subTest(price=price):
                room = make_room(price=price)
                city = make_city(hotels=[make_hotel(rooms=[room])])
                self.assertRejected(make_dataset(city), "ongeldige prijs")

    def test_highest_allowed_price_is_accepted(self):
        room = make_room(price="9999.99")
        self.write_dataset(make_dataset(make_city(hotels=[make_hotel(rooms=[room])])))
        run_command("--noinput")
        self.assertEqual(Room.objects.get().price, Decimal("9999.99"))

    def test_price_that_rounds_to_zero_is_rejected(self):
        # 0.004 passes "price > 0" but is stored as 0.00.
        room = make_room(price=0.004)
        city = make_city(hotels=[make_hotel(rooms=[room])])
        self.assertRejected(make_dataset(city), "valt buiten het bereik")

    def test_photo_that_does_not_exist(self):
        cases = [
            make_city(hotels=[make_hotel(image="demo/hotels/bestaat-niet.jpg")]),
            make_city(
                hotels=[make_hotel(rooms=[make_room(image="demo/rooms/weg.jpg")])]
            ),
        ]
        for city in cases:
            with self.subTest(city=city):
                self.assertRejected(make_dataset(city), "Deze foto's ontbreken")

    def test_photo_path_that_is_a_directory_is_rejected(self):
        city = make_city(hotels=[make_hotel(image="demo/hotels")])
        self.assertRejected(make_dataset(city), "Deze foto's ontbreken")

    def test_photo_path_outside_the_static_files_gives_command_error(self):
        for image in ["/etc/hosts", "../../../../../etc/hosts"]:
            with self.subTest(image=image):
                city = make_city(hotels=[make_hotel(image=image)])
                self.assertRejected(make_dataset(city))

    def test_hotel_without_rooms(self):
        hotel_without_rooms_key = make_hotel()
        del hotel_without_rooms_key["rooms"]
        for hotel in [make_hotel(rooms=[]), hotel_without_rooms_key]:
            with self.subTest(hotel=hotel):
                city = make_city(hotels=[hotel])
                self.assertRejected(make_dataset(city), "heeft geen kamers")

    def test_city_without_hotels(self):
        self.assertRejected(make_dataset(make_city(hotels=[])), "heeft geen hotels")

    def test_duplicate_city_id(self):
        dataset = make_dataset(make_city(), make_city(name="Andere stad"))
        self.assertRejected(dataset, "Stad TST komt meer dan eens voor")

    def test_error_in_the_last_hotel_rejects_the_whole_file(self):
        last_city = make_city(
            city_id="LST",
            hotels=[make_hotel(), make_hotel(rooms=[make_room(price=-1)])],
        )
        self.assertRejected(make_dataset(make_city(), last_city), "Hotel 2 in LST")

    def test_invalid_file_is_reported_before_asking_for_confirmation(self):
        self.write_dataset(make_dataset(make_city(hotels=[])))
        with mock.patch("builtins.input", return_value="ja") as prompt:
            with self.assertRaises(CommandError):
                run_command()
        prompt.assert_not_called()
        self.assertExistingDataUntouched()

    def test_wrongly_structured_file_gives_command_error(self):
        cases = [
            {"cities": ["AMS"]},
            make_dataset(make_city(hotels=["Amstel Hotel"])),
            make_dataset(make_city(hotels={"naam": "Amstel Hotel"})),
            make_dataset(make_city(hotels=[make_hotel(rooms=["Double"])])),
            make_dataset(make_city(hotels=[make_hotel(rooms="Double")])),
            make_dataset(
                make_city(hotels=[make_hotel(rooms=[make_room(room_type=["Double"])])])
            ),
        ]
        for data in cases:
            with self.subTest(data=data):
                self.assertRejected(data)


class DemoDataQualityTests(TestCase):
    """Checks on the real hotels.json, after loading it."""

    @classmethod
    def setUpTestData(cls):
        run_command("--noinput")

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with open(load_demo_data.DATA_FILE, encoding="utf-8") as fh:
            cls.json_data = json.load(fh)

    def test_number_of_hotels_per_city_matches_the_file(self):
        expected = {
            city["city_id"]: len(city["hotels"]) for city in self.json_data["cities"]
        }
        actual = dict(
            City.objects.annotate(n=Count("test")).values_list("city_id", "n")
        )
        self.assertEqual(actual, expected)
        self.assertEqual(sum(actual.values()), DEMO_HOTEL_COUNT)

    def test_every_hotel_is_available_and_has_an_available_room(self):
        self.assertFalse(Hotel.objects.filter(is_available=False).exists())
        self.assertFalse(Room.objects.filter(is_available=False).exists())
        without_rooms = Hotel.objects.annotate(
            available_rooms=Count("room", filter=Q(room__is_available=True))
        ).filter(available_rooms=0)
        self.assertEqual(list(without_rooms.values_list("name", flat=True)), [])

    def test_all_prices_are_positive(self):
        self.assertFalse(Room.objects.filter(price__lte=0).exists())
        self.assertFalse(Hotel.objects.filter(price__lte=0).exists())

    def test_hotel_price_is_the_lowest_room_price(self):
        wrong = [
            (hotel.hotel_id, hotel.price, hotel.lowest)
            for hotel in Hotel.objects.annotate(lowest=Min("room__price"))
            if hotel.price != hotel.lowest
        ]
        self.assertEqual(wrong, [])

    def test_hotel_id_is_city_id_plus_two_digit_sequence_number(self):
        numbers = defaultdict(list)
        for hotel in Hotel.objects.select_related("city"):
            city_id = hotel.city.city_id
            with self.subTest(hotel_id=hotel.hotel_id):
                self.assertRegex(hotel.hotel_id, rf"^{city_id}\d{{2}}$")
            numbers[city_id].append(int(hotel.hotel_id.removeprefix(city_id)))
        for city_id, found in numbers.items():
            with self.subTest(city_id=city_id):
                self.assertEqual(sorted(found), list(range(1, len(found) + 1)))
        self.assertEqual(
            Hotel.objects.values("hotel_id").distinct().count(), DEMO_HOTEL_COUNT
        )

    def test_all_images_exist_under_static(self):
        images = set(Hotel.objects.values_list("image", flat=True))
        images |= set(Room.objects.values_list("image", flat=True))
        self.assertTrue(images)
        missing = sorted(
            image
            for image in images
            if not image.startswith("demo/") or not (STATIC_DIR / image).is_file()
        )
        self.assertEqual(missing, [])

    def test_no_duplicate_hotel_names_within_a_city(self):
        names = Counter(
            (city_id, name.strip().casefold())
            for city_id, name in Hotel.objects.values_list("city__city_id", "name")
        )
        self.assertEqual([key for key, count in names.items() if count > 1], [])

    def test_rooms_have_a_known_type_and_a_description(self):
        room_types = {value for value, _label in ROOM_CHOICES}
        self.assertFalse(Room.objects.exclude(room_type__in=room_types).exists())
        self.assertFalse(Room.objects.filter(description__exact="").exists())

    def test_amstel_hotel_has_its_address(self):
        hotel = Hotel.objects.get(hotel_id="AMS01")
        self.assertEqual(hotel.name, "Amstel Hotel")
        self.assertEqual(hotel.address, "Prof. Tulpplein 1")

    def test_address_is_street_and_housenumber_from_the_file(self):
        expected = {}
        for city in self.json_data["cities"]:
            for number, hotel in enumerate(city["hotels"], start=1):
                parts = (hotel.get("street"), hotel.get("housenumber"))
                address = " ".join(part.strip() for part in parts if part)
                expected[f"{city['city_id']}{number:02d}"] = address.strip()
        self.assertEqual(
            dict(Hotel.objects.values_list("hotel_id", "address")), expected
        )
        self.assertFalse(Hotel.objects.filter(address="").exists())

    def test_every_hotel_in_the_file_refers_to_its_openstreetmap_object(self):
        self.assertIn("openstreetmap.org/copyright", self.json_data["license"].lower())
        for city in self.json_data["cities"]:
            for hotel in city["hotels"]:
                with self.subTest(hotel=hotel["name"]):
                    self.assertRegex(hotel["osm"], r"^(node|way|relation)/\d+$")


class DemoDataPagesTests(TestCase):
    """The public pages work with the demo data."""

    @classmethod
    def setUpTestData(cls):
        run_command("--noinput")
        cls.hotel = Hotel.objects.select_related("city").get(hotel_id="AMS01")

    def setUp(self):
        # The rate limits count in the cache, which all tests share.
        cache.clear()

    def page_urls(self):
        return [
            reverse("index"),
            reverse("hotel"),
            reverse("stad", args=["Amsterdam"]),
            reverse("hotel_detail", args=[self.hotel.pk]),
            reverse("order", args=[self.hotel.pk]),
        ]

    def test_pages_render(self):
        for url in self.page_urls():
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_footer_credits_openstreetmap_on_every_page(self):
        for url in self.page_urls():
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertRegex(response.content.decode(), OSM_CREDIT)

    def test_home_and_hotels_page_list_all_cities_with_hotel_counts(self):
        counts = dict(City.objects.annotate(n=Count("test")).values_list("name", "n"))
        for url in [reverse("index"), reverse("hotel")]:
            response = self.client.get(url)
            for name, count in counts.items():
                with self.subTest(url=url, city=name):
                    self.assertContains(response, reverse("stad", args=[name]))
                    self.assertContains(response, f"{count} hotels")

    def test_every_city_page_renders_all_its_pages(self):
        for city in City.objects.annotate(n=Count("test")):
            url = reverse("stad", args=[city.name])
            last_page = (city.n + 11) // 12
            with self.subTest(city=city.name):
                response = self.client.get(url)
                self.assertContains(response, f"{city.n} hotels")
                response = self.client.get(url, {"page": last_page})
                self.assertEqual(response.status_code, 200)

    def test_city_page_shows_hotels_with_their_lowest_price(self):
        response = self.client.get(reverse("stad", args=["Amsterdam"]))
        first = (
            Hotel.objects.filter(city__name="Amsterdam")
            .order_by("name")
            .select_related("city")
            .first()
        )
        self.assertContains(response, escape(first.name))
        self.assertContains(response, f"€ {first.price:.0f}")
        self.assertContains(response, f'src="/static/{first.image.name}"')

    def test_every_hotel_detail_page_renders(self):
        for hotel in Hotel.objects.prefetch_related("room"):
            with self.subTest(hotel=hotel.hotel_id):
                response = self.client.get(reverse("hotel_detail", args=[hotel.pk]))
                self.assertContains(response, escape(hotel.name))
                for room in hotel.room.all():
                    self.assertContains(response, f"€ {room.price:.0f}")

    def test_hotel_detail_page_shows_the_address(self):
        response = self.client.get(reverse("hotel_detail", args=[self.hotel.pk]))
        self.assertContains(response, ">Prof. Tulpplein 1, Amsterdam</dd>")

    def test_photos_on_detail_page_are_existing_static_files(self):
        response = self.client.get(reverse("hotel_detail", args=[self.hotel.pk]))
        sources = re.findall(r'src="/static/(demo/[^"]+)"', response.content.decode())
        self.assertGreaterEqual(len(sources), 1 + self.hotel.room.count())
        for source in sources:
            with self.subTest(source=source):
                self.assertTrue((STATIC_DIR / source).is_file())

    def test_checkout_lists_the_rooms_of_the_hotel(self):
        response = self.client.get(reverse("order", args=[self.hotel.pk]))
        self.assertContains(response, "Kies je kamer")
        self.assertContains(response, escape(self.hotel.name))
        for room in self.hotel.room.all():
            self.assertContains(response, f'value="{room.pk}"')
            self.assertContains(response, f"€ {room.price:.0f}")

    def test_demo_hotel_can_be_booked(self):
        url = reverse("order", args=[self.hotel.pk])
        room = self.hotel.room.order_by("price").first()
        start = date.today() + timedelta(days=14)
        end = start + timedelta(days=2)
        self.client.get(url)

        steps = [
            (
                "stay",
                {"start_date": start, "end_date": end, "room": room.pk},
            ),
            (
                "guest",
                {
                    "first_name": "Alex",
                    "last_name": "de Landgraaf",
                    "email": "alex@example.com",
                    "address": "Keizersgracht 117",
                    "zipcode": "1015 CJ",
                    "country": "NL",
                },
            ),
            ("confirm", {"terms": "on"}),
        ]
        for step, data in steps:
            payload = {"checkout-current_step": step}
            payload.update({f"{step}-{key}": value for key, value in data.items()})
            response = self.client.post(url, payload)

        order = Order.objects.get()
        detail_url = reverse("bookings:detail", kwargs={"token": order.token})
        self.assertRedirects(response, detail_url)
        self.assertEqual(order.hotel, self.hotel)
        self.assertEqual(order.room, room)
        self.assertEqual(order.total_price, room.price * 2)

        response = self.client.get(detail_url)
        self.assertContains(response, escape(self.hotel.name))
        self.assertContains(response, "2 nachten")
