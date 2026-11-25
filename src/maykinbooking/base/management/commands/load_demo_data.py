"""
Replace all cities, hotels and rooms with the demo data in ``demo_data/hotels.json``.

The dataset is a fixed file in the repository (generated once from OpenStreetMap,
see ``demo_data/README.md``), so loading it never touches the network and the
result is the same every time the command runs.
"""
import json
import os
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path, PurePosixPath

from django.conf import settings
from django.contrib.staticfiles import finders
from django.core.exceptions import SuspiciousFileOperation
from django.core.management.base import BaseCommand, CommandError
from django.db import connections, transaction

from maykinbooking.base.models import (
    ROOM_CHOICES,
    City,
    Hotel,
    Order,
    Review,
    Room,
    upload_storage,
)

DATA_FILE = Path(__file__).resolve().parents[2] / "demo_data" / "hotels.json"

ROOM_TYPES = {value for value, _label in ROOM_CHOICES}
MAX_PRICE = Decimal("9999.99")  # Room.price / Hotel.price: max_digits=6, 2 decimals
MAX_ADDRESS_LENGTH = Hotel._meta.get_field("address").max_length

CONFIRM_PROMPT = """Je staat op het punt de demodata te laden.

Dit verwijdert alle steden, hotels, kamers, boekingen en reviews uit de
database '{database}' en vervangt ze door de demodata uit {path}.
Gebruikers en accounts blijven bestaan. Dit kan niet ongedaan worden gemaakt.

Typ 'ja' om door te gaan of 'nee' om te annuleren: """


def _object(value, context):
    if not isinstance(value, dict):
        raise CommandError(f"{context}: verwacht een object, kreeg {value!r}.")
    return value


def _list(data, key, context):
    value = data.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise CommandError(f"{context}: '{key}' moet een lijst zijn.")
    return value


def _required_text(data, key, context, max_length=None):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CommandError(f"{context}: '{key}' ontbreekt of is leeg.")
    if max_length and len(value) > max_length:
        raise CommandError(f"{context}: '{key}' is langer dan {max_length} tekens.")
    return value


def _optional_text(data, key, context):
    value = data.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise CommandError(f"{context}: '{key}' moet tekst zijn.")
    return value.strip()


def _address(data, context):
    """``"<street> <housenumber>"``; the house number may be missing or empty."""
    parts = (_optional_text(data, key, context) for key in ("street", "housenumber"))
    address = " ".join(part for part in parts if part)
    if len(address) > MAX_ADDRESS_LENGTH:
        raise CommandError(
            f"{context}: adres ('street' + 'housenumber') is langer dan "
            f"{MAX_ADDRESS_LENGTH} tekens."
        )
    return address


def _image(data, context):
    image = _required_text(data, "image", context, max_length=100)
    parts = PurePosixPath(image).parts
    if image.startswith("/") or "\\" in image or ".." in parts:
        raise CommandError(f"{context}: ongeldig fotopad {image!r}.")
    return image


def _price(value, context):
    try:
        price = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise CommandError(f"{context}: ongeldige prijs {value!r}.")
    try:
        # Round first: 0.004 would pass "> 0" but is stored as 0.00.
        price = price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation:  # NaN, Infinity or far too many digits
        price = None
    if price is None or not price.is_finite() or not 0 < price <= MAX_PRICE:
        raise CommandError(f"{context}: prijs {value!r} valt buiten het bereik.")
    return price


def _image_exists(path):
    """True if ``path`` is an existing file in the static files (not a folder)."""
    try:
        found = finders.find(path)
        if found and os.path.isfile(found):
            return True
        return upload_storage.exists(path) and os.path.isfile(upload_storage.path(path))
    except SuspiciousFileOperation:
        return False


def load_dataset(path):
    """
    Read and validate the dataset before anything is deleted.

    Returns a list of cities, each with its hotels and rooms, with prices
    converted to ``Decimal``.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise CommandError(f"Kan {path} niet lezen: {exc}")

    cities = data.get("cities") if isinstance(data, dict) else None
    if not isinstance(cities, list) or not cities:
        raise CommandError(f"{path} bevat geen steden.")

    result, images, city_ids = [], set(), set()
    for city_number, city in enumerate(cities, start=1):
        city = _object(city, f"Stad {city_number}")
        city_id = _required_text(city, "city_id", "Stad", max_length=100)
        if city_id in city_ids:
            raise CommandError(f"Stad {city_id} komt meer dan eens voor.")
        city_ids.add(city_id)
        hotels = []
        for number, hotel in enumerate(_list(city, "hotels", city_id), start=1):
            context = f"Hotel {number} in {city_id}"
            hotel = _object(hotel, context)
            rooms = []
            for room in _list(hotel, "rooms", context):
                room_context = f"{context}, kamer"
                room = _object(room, room_context)
                room_type = room.get("room_type")
                if not isinstance(room_type, str) or room_type not in ROOM_TYPES:
                    raise CommandError(
                        f"{room_context}: onbekend kamertype {room_type!r}."
                    )
                rooms.append(
                    {
                        "room_type": room_type,
                        "price": _price(room.get("price"), room_context),
                        "description": _required_text(
                            room, "description", room_context
                        ),
                        "image": _image(room, room_context),
                    }
                )
            if not rooms:
                raise CommandError(f"{context} heeft geen kamers.")
            hotels.append(
                {
                    "name": _required_text(hotel, "name", context, max_length=100),
                    "address": _address(hotel, context),
                    "description": _required_text(hotel, "description", context),
                    "terms": _required_text(hotel, "terms", context),
                    "image": _image(hotel, context),
                    "rooms": rooms,
                }
            )
            images.add(hotels[-1]["image"])
            images.update(room["image"] for room in rooms)
        if not hotels:
            raise CommandError(f"Stad {city_id} heeft geen hotels.")
        result.append(
            {
                "city_id": city_id,
                "name": _required_text(city, "name", city_id, max_length=100),
                "hotels": hotels,
            }
        )

    missing = sorted(image for image in images if not _image_exists(image))
    if missing:
        raise CommandError(
            "Deze foto's ontbreken in de static files: " + ", ".join(missing)
        )
    return result


def _delete_all(model):
    _total, per_model = model.objects.all().delete()
    return per_model.get(model._meta.label, 0)


class Command(BaseCommand):
    help = (
        "Verwijdert alle steden, hotels, kamers, boekingen en reviews en laadt "
        "daarna de demodata uit base/demo_data/hotels.json."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--noinput",
            "--no-input",
            action="store_false",
            dest="interactive",
            help="Vraag niet om bevestiging.",
        )

    def handle(self, *args, **options):
        if getattr(settings, "ENVIRONMENT", "") == "production":
            raise CommandError("load_demo_data is niet bedoeld voor productie.")

        cities = load_dataset(DATA_FILE)

        if options["interactive"]:
            database = connections[Hotel.objects.db].settings_dict["NAME"]
            prompt = CONFIRM_PROMPT.format(database=database, path=DATA_FILE.name)
            if input(prompt).strip().lower() not in ("ja", "yes"):
                self.stdout.write("Geannuleerd, er is niets gewijzigd.")
                return

        with transaction.atomic():
            deleted = {
                "boekingen": _delete_all(Order),
                "reviews": _delete_all(Review),
                "kamers": _delete_all(Room),
                "hotels": _delete_all(Hotel),
                "steden": _delete_all(City),
            }
            created = self._create(cities)

        if options["verbosity"] >= 1:
            removed = ", ".join(f"{count} {label}" for label, count in deleted.items())
            self.stdout.write(f"Verwijderd: {removed}.")
            self.stdout.write(
                self.style.SUCCESS(
                    "Demodata geladen: {steden} steden, {hotels} hotels, "
                    "{kamers} kamers.".format(**created)
                )
            )

    def _create(self, cities):
        counts = {"steden": 0, "hotels": 0, "kamers": 0}
        for city_data in cities:
            city = City.objects.create(
                city_id=city_data["city_id"], name=city_data["name"]
            )
            counts["steden"] += 1
            for number, hotel_data in enumerate(city_data["hotels"], start=1):
                rooms = hotel_data["rooms"]
                hotel = Hotel.objects.create(
                    city=city,
                    # Same convention as dashboard.forms.next_hotel_id: AMS01, AMS02...
                    hotel_id=f"{city.city_id}{number:02d}",
                    name=hotel_data["name"],
                    address=hotel_data["address"],
                    image=hotel_data["image"],
                    description=hotel_data["description"],
                    terms=hotel_data["terms"],
                    price=min(room["price"] for room in rooms),
                    is_available=True,
                )
                Room.objects.bulk_create(
                    Room(
                        hotel=hotel,
                        room_type=room["room_type"],
                        price=room["price"],
                        description=room["description"],
                        image=room["image"],
                        is_available=True,
                    )
                    for room in rooms
                )
                counts["hotels"] += 1
                counts["kamers"] += len(rooms)
        return counts
