"""
Edge cases of the CSV export of the bookings (US3.1, testpunt 12): combined
filters, the same result as the list, formulas in every text column, separators
and newlines in values, invalid filter values, many rows and the number of
queries.
"""
import csv
import io
from datetime import date, timedelta
from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from freezegun import freeze_time

from maykinbooking.accounts.tests.factories import StaffUserFactory
from maykinbooking.base.models import OrderStatus
from maykinbooking.base.tests.factories import (
    CityFactory,
    HotelFactory,
    OrderFactory,
    RoomFactory,
)

# Monday 1 March 2027, 10:00 in Amsterdam.
NOW = "2027-03-01T09:00:00Z"
TODAY = date(2027, 3, 1)


@freeze_time(NOW)
class ExportTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = StaffUserFactory()
        cls.amsterdam = CityFactory(name="Amsterdam")
        cls.utrecht = CityFactory(name="Utrecht")
        cls.gracht = HotelFactory(
            city=cls.amsterdam, name="Hotel de Gracht", is_available=True
        )
        cls.dam = HotelFactory(city=cls.amsterdam, name="Hotel Dam", is_available=True)
        cls.dom = HotelFactory(city=cls.utrecht, name="Hotel Dom", is_available=True)
        cls.rooms = {
            hotel: RoomFactory(hotel=hotel, room_type="Double", price=Decimal("100"))
            for hotel in (cls.gracht, cls.dam, cls.dom)
        }

    def setUp(self):
        super().setUp()
        self.client.force_login(self.staff)

    def order(self, hotel=None, start_in=7, nights=2, **fields):
        hotel = hotel or self.gracht
        start = TODAY + timedelta(days=start_in)
        return OrderFactory(
            hotel=hotel,
            room=self.rooms[hotel],
            start_date=start,
            end_date=start + timedelta(days=nights),
            **fields,
        )

    def export(self, **params):
        response = self.client.get(reverse("dashboard:booking_export"), params)
        self.assertEqual(response.status_code, 200)
        content = b"".join(response.streaming_content).decode("utf-8")
        self.assertEqual(content.count("﻿"), 1)
        self.assertTrue(content.startswith("﻿"))
        rows = list(csv.reader(io.StringIO(content[1:]), delimiter=";"))
        header, *records = rows
        return [dict(zip(header, record)) for record in records]

    def listed(self, **params):
        response = self.client.get(reverse("dashboard:booking_list"), params)
        self.assertEqual(response.status_code, 200)
        return [order.pk for order in response.context["orders"]]


class CombinedFiltersTests(ExportTestCase):
    def setUp(self):
        super().setUp()
        self.match = [
            self.order(self.gracht, start_in=3, status=OrderStatus.CANCELLED),
            self.order(self.dam, start_in=5, status=OrderStatus.CANCELLED),
        ]
        # Each one fails one filter.
        self.order(self.dom, start_in=4, status=OrderStatus.CANCELLED)  # city
        self.order(self.gracht, start_in=4)  # status
        self.order(self.gracht, start_in=1, status=OrderStatus.CANCELLED)  # from
        self.order(self.dam, start_in=10, status=OrderStatus.CANCELLED)  # until
        self.params = {
            "stad": self.amsterdam.pk,
            "status": OrderStatus.CANCELLED,
            "aankomst_van": (TODAY + timedelta(days=2)).isoformat(),
            "aankomst_tot": (TODAY + timedelta(days=5)).isoformat(),
        }

    def test_export_has_exactly_the_rows_of_the_list(self):
        expected = [order.pk for order in self.match]
        self.assertEqual(self.listed(**self.params), expected)
        records = self.export(**self.params)
        self.assertEqual([int(record["Nummer"]) for record in records], expected)
        self.assertEqual({record["Status"] for record in records}, {"Geannuleerd"})
        self.assertEqual(
            [record["Aankomst"] for record in records], ["2027-03-04", "2027-03-06"]
        )

    def test_hotel_and_search_narrow_it_down_further(self):
        params = {**self.params, "hotel": self.dam.pk}
        self.assertEqual(
            [int(record["Nummer"]) for record in self.export(**params)],
            [self.match[1].pk],
        )
        target = self.match[0]
        records = self.export(**self.params, q=f"#{target.pk}")
        self.assertEqual([int(record["Nummer"]) for record in records], [target.pk])

    def test_arrival_until_is_inclusive_and_past_arrivals_can_be_exported(self):
        past = self.order(self.gracht, start_in=-20, status=OrderStatus.CANCELLED)
        params = {
            **self.params,
            "aankomst_van": (TODAY - timedelta(days=30)).isoformat(),
            "aankomst_tot": (TODAY + timedelta(days=3)).isoformat(),
        }
        numbers = [int(record["Nummer"]) for record in self.export(**params)]
        self.assertEqual(numbers[0], past.pk)
        self.assertIn(self.match[0].pk, numbers)  # arrives on the 4th
        self.assertNotIn(self.match[1].pk, numbers)  # arrives on the 6th


class InjectionTests(ExportTestCase):
    def test_formulas_are_neutralised_in_every_text_column(self):
        city = CityFactory(name="=1+1")
        hotel = HotelFactory(city=city, name="@SUM(A1:A9)", is_available=True)
        self.rooms[hotel] = RoomFactory(hotel=hotel)
        self.order(
            hotel,
            first_name='=HYPERLINK("http://evil.example","klik")',
            last_name="+31 6 12345678",
            email="-1@example.com",
        )
        self.order(hotel, first_name="\t=1+1", last_name="\r=1+1")

        first, second = self.export(stad=city.pk)

        self.assertEqual(
            first["Voornaam"], """'=HYPERLINK("http://evil.example","klik")"""
        )
        self.assertEqual(first["Achternaam"], "'+31 6 12345678")
        self.assertEqual(first["E-mail"], "'-1@example.com")
        self.assertEqual(first["Hotel"], "'@SUM(A1:A9)")
        self.assertEqual(first["Stad"], "'=1+1")
        self.assertEqual(second["Voornaam"], "'\t=1+1")
        self.assertEqual(second["Achternaam"], "'\r=1+1")
        for record in (first, second):
            for column, value in record.items():
                with self.subTest(column=column, value=value):
                    self.assertFalse(value.startswith(("=", "+", "-", "@", "\t", "\r")))

    def test_separators_quotes_and_newlines_stay_inside_their_cell(self):
        self.order(first_name='Jan "JJ"', last_name="Jansen; de Vries\nregel 2")

        [record] = self.export()

        self.assertEqual(record["Voornaam"], 'Jan "JJ"')
        self.assertEqual(record["Achternaam"], "Jansen; de Vries\nregel 2")
        self.assertEqual(record["Hotel"], "Hotel de Gracht")
        self.assertEqual(len(record), 14)

    def test_formula_in_the_search_term_does_nothing(self):
        self.order(first_name="=cmd")
        records = self.export(q="=cmd")
        self.assertEqual([record["Voornaam"] for record in records], ["'=cmd"])


class RobustnessTests(ExportTestCase):
    def test_invalid_filter_values_are_ignored(self):
        upcoming = self.order(start_in=2)
        self.order(start_in=-2)
        params = {
            "aankomst_van": "gisteren",
            "aankomst_tot": "2027-02-30",
            "stad": "abc",
            "hotel": "999999",
            "status": "bestaat-niet",
            "snel": "onzin",
            "mail": "onzin",
            "q": "",
        }
        records = self.export(**params)
        # The default period remains: arrivals from today.
        self.assertEqual([int(record["Nummer"]) for record in records], [upcoming.pk])
        response = self.client.get(reverse("dashboard:booking_list"), params)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)

    def test_booked_at_is_the_time_in_amsterdam(self):
        self.order()
        [record] = self.export()
        self.assertEqual(record["Geboekt op"], "2027-03-01 10:00")

    def test_more_rows_than_one_chunk_of_the_iterator(self):
        for _ in range(510):
            self.order(nights=1, first_name="Gast")
        records = self.export()
        self.assertEqual(len(records), 510)
        self.assertEqual(len({record["Nummer"] for record in records}), 510)

    def count_queries(self, number_of_orders):
        for index in range(number_of_orders):
            hotel = (self.gracht, self.dam, self.dom)[index % 3]
            self.order(hotel, start_in=index % 5)
        with CaptureQueriesContext(connection) as queries:
            self.export()
        return len(queries)

    def test_number_of_queries_does_not_grow_with_the_rows(self):
        few = self.count_queries(3)
        self.assertEqual(self.count_queries(30), few)
