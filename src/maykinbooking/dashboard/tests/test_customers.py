"""
Customers in the dashboard (US4.1): an e-mail address, trimmed and in lower
case, over all bookings. The list, search and sorting, the page of a customer
and the CSV export.
"""
import csv
import io
from datetime import date, timedelta
from decimal import Decimal

from django.db import connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from freezegun import freeze_time

from maykinbooking.accounts.tests.factories import ManagerUserFactory
from maykinbooking.base.models import OrderStatus
from maykinbooking.base.tests.factories import (
    CityFactory,
    HotelFactory,
    OrderFactory,
    RoomFactory,
)
from maykinbooking.billing.services.invoicing import issue_invoice

from ..customers import attach_names, customer_orders, customers

TODAY = date(2027, 3, 1)


@freeze_time("2027-03-01T09:00:00Z")
class CustomerTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = ManagerUserFactory()
        cls.hotel = HotelFactory(
            city=CityFactory(name="Amsterdam"), name="Hotel Gracht"
        )
        cls.room = RoomFactory(hotel=cls.hotel, price=Decimal("100.00"))

    def setUp(self):
        super().setUp()
        self.client.force_login(self.staff)

    def order(self, email, *, start_in=-10, nights=2, **fields):
        start = TODAY + timedelta(days=start_in)
        return OrderFactory(
            hotel=self.hotel,
            room=self.room,
            email=email,
            start_date=start,
            end_date=start + timedelta(days=nights),
            **fields,
        )

    def get(self, **params):
        return self.client.get(reverse("dashboard:customer_list"), params)

    def rows(self, response):
        return {row["key"]: row for row in response.context["customers"]}


class AggregationTests(CustomerTestCase):
    def test_one_customer_over_all_bookings(self):
        # The same address with other capitals and spaces around it.
        self.order(
            " Alex@Example.com", start_in=-30, first_name="Alex", last_name="Oud"
        )
        self.order("alex@example.com ", start_in=-5, status=OrderStatus.CANCELLED)
        self.order("ALEX@EXAMPLE.COM", start_in=-2, nights=3)
        newest = self.order(
            "alex@example.com", start_in=20, first_name="Alexander", last_name="Nieuw"
        )
        # Not counted: bookings that expired or still wait for payment.
        self.order("alex@example.com", status=OrderStatus.EXPIRED)
        self.order("alex@example.com", status=OrderStatus.PENDING_PAYMENT)
        self.order("sam@example.com", start_in=-1)

        response = self.get()

        rows = self.rows(response)
        self.assertEqual(set(rows), {"alex@example.com", "sam@example.com"})
        alex = rows["alex@example.com"]
        self.assertEqual(alex["bookings"], 4)
        self.assertEqual(alex["cancellations"], 1)
        # Confirmed only: 2 + 3 + 2 nights of 100, the future stay included.
        self.assertEqual(alex["spent"], Decimal("700.00"))
        # The last arrival up to today, not the future one.
        self.assertEqual(alex["last_stay"], TODAY - timedelta(days=2))
        self.assertEqual(alex["last_order_id"], newest.pk)
        # The name of the newest booking.
        self.assertEqual(alex["name"], "Alexander Nieuw")
        self.assertContains(response, "2 klanten")
        self.assertContains(response, "€ 700,00")
        self.assertContains(
            response, reverse("dashboard:customer_detail", args=[newest.pk])
        )

    def test_customer_without_a_stay_yet(self):
        self.order("nieuw@example.com", start_in=10)
        self.order("geannuleerd@example.com", status=OrderStatus.CANCELLED)

        rows = self.rows(self.get())

        self.assertIsNone(rows["nieuw@example.com"]["last_stay"])
        self.assertEqual(rows["geannuleerd@example.com"]["spent"], Decimal("0.00"))
        self.assertIsNone(rows["geannuleerd@example.com"]["last_stay"])

    def test_a_stay_that_starts_today_counts(self):
        self.order("vandaag@example.com", start_in=0)
        self.assertEqual(
            self.rows(self.get())["vandaag@example.com"]["last_stay"], TODAY
        )


class SearchTests(CustomerTestCase):
    def setUp(self):
        super().setUp()
        self.order("alex@example.com", first_name="Alex", last_name="Jansen")
        self.order(" ALEX@example.com", first_name="Alexander", last_name="de Groot")
        self.order("alex@example.com", status=OrderStatus.CANCELLED, first_name="A.")
        self.sam = self.order("sam@example.com", first_name="Sam", last_name="Jansen")

    def test_search_does_not_change_the_counts(self):
        # Only one booking of Alex has "Groot", but all of them count.
        rows = self.rows(self.get(q="groot"))
        self.assertEqual(list(rows), ["alex@example.com"])
        self.assertEqual(rows["alex@example.com"]["bookings"], 3)
        self.assertEqual(rows["alex@example.com"]["cancellations"], 1)

    def test_search_on_name_email_and_booking_number(self):
        self.assertEqual(
            set(self.rows(self.get(q="jansen"))),
            {"alex@example.com", "sam@example.com"},
        )
        self.assertEqual(
            list(self.rows(self.get(q="SAM@EXAMPLE"))), ["sam@example.com"]
        )
        self.assertEqual(
            list(self.rows(self.get(q=f"#{self.sam.pk}"))), ["sam@example.com"]
        )
        self.assertEqual(list(self.rows(self.get(q="sam jansen"))), ["sam@example.com"])
        response = self.get(q="niemand")
        self.assertEqual(list(self.rows(response)), [])
        self.assertContains(response, "Geen klanten gevonden")

    def test_expired_bookings_are_not_searched(self):
        self.order("weg@example.com", first_name="Verlopen", status=OrderStatus.EXPIRED)
        self.assertEqual(list(self.rows(self.get(q="verlopen"))), [])


class SortTests(CustomerTestCase):
    def setUp(self):
        super().setUp()
        self.order("b@example.com", start_in=-20)
        self.order("b@example.com", start_in=-19, status=OrderStatus.CANCELLED)
        self.order("a@example.com", start_in=-3, nights=5)
        self.order("c@example.com", start_in=5)

    def keys(self, sort):
        return [row["key"] for row in self.get(sort=sort).context["customers"]]

    def test_sort_on_every_column(self):
        cases = {
            "email": ["a@example.com", "b@example.com", "c@example.com"],
            "-email": ["c@example.com", "b@example.com", "a@example.com"],
            "-boekingen": ["b@example.com", "a@example.com", "c@example.com"],
            "boekingen": ["a@example.com", "c@example.com", "b@example.com"],
            "-annuleringen": ["b@example.com", "a@example.com", "c@example.com"],
            "-besteed": ["a@example.com", "b@example.com", "c@example.com"],
            "besteed": ["b@example.com", "c@example.com", "a@example.com"],
            # Customers without a stay yet come last.
            "-laatste_verblijf": ["a@example.com", "b@example.com", "c@example.com"],
            "laatste_verblijf": ["c@example.com", "b@example.com", "a@example.com"],
            # The newest booking first (the default).
            "-laatste_boeking": ["c@example.com", "a@example.com", "b@example.com"],
        }
        for sort, expected in cases.items():
            with self.subTest(sort=sort):
                self.assertEqual(self.keys(sort), expected)

    def test_unknown_sort_is_the_default(self):
        default = self.keys("-laatste_boeking")
        for sort in ("pk", "email; DROP TABLE", "-name", ""):
            with self.subTest(sort=sort):
                self.assertEqual(self.keys(sort), default)

    def test_column_headers_sort_and_reverse(self):
        response = self.get(sort="-besteed", q="example")
        links = {str(link["label"]): link for link in response.context["sort_links"]}
        self.assertEqual(links["Totaal besteed"]["direction"], "descending")
        self.assertEqual(links["Totaal besteed"]["query"], "sort=besteed&q=example")
        self.assertEqual(links["Boekingen"]["query"], "sort=-boekingen&q=example")
        self.assertEqual(links["Klant"]["query"], "sort=email&q=example")
        self.assertContains(response, 'aria-sort="descending"')


class QueryCountTests(CustomerTestCase):
    def count_queries(self):
        with CaptureQueriesContext(connection) as context:
            response = self.get()
            self.assertEqual(response.status_code, 200)
        return len(context.captured_queries), len(response.context["customers"])

    def test_the_same_number_of_queries_for_2_and_20_customers(self):
        for number in range(2):
            self.order(f"klant{number}@example.com")
            self.order(f"KLANT{number}@example.com ", status=OrderStatus.CANCELLED)
        few = self.count_queries()
        for number in range(2, 20):
            self.order(f"klant{number}@example.com")
            self.order(f"klant{number}@example.com", status=OrderStatus.CANCELLED)
        many = self.count_queries()

        self.assertEqual((few[1], many[1]), (2, 20))
        self.assertEqual(few[0], many[0])

    def test_three_queries_for_the_customers(self):
        for number in range(5):
            self.order(f"klant{number}@example.com")
        # The count of the paginator, the page and the names.
        with self.assertNumQueries(3):
            rows = customers(q="klant")
            self.assertEqual(rows.count(), 5)
            attach_names(rows[:25])


class CustomerDetailTests(CustomerTestCase):
    def test_bookings_and_invoices_of_the_customer(self):
        first = self.order(
            " Alex@Example.com", start_in=-30, first_name="Alex", last_name="Oud"
        )
        cancelled = self.order("alex@example.com", status=OrderStatus.CANCELLED)
        expired = self.order("alex@example.com", status=OrderStatus.EXPIRED)
        newest = self.order(
            "alex@example.com", start_in=10, first_name="Alex", last_name="Nieuw"
        )
        other = self.order("sam@example.com", first_name="Sam")
        with transaction.atomic():
            invoice = issue_invoice(first, today=TODAY)
            other_invoice = issue_invoice(other, today=TODAY)

        response = self.client.get(
            reverse("dashboard:customer_detail", args=[cancelled.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [order.pk for order in response.context["orders"]],
            [newest.pk, expired.pk, cancelled.pk, first.pk],
        )
        self.assertEqual(response.context["name"], "Alex Nieuw")
        self.assertEqual(response.context["email"], "alex@example.com")
        summary = response.context["summary"]
        self.assertEqual((summary["bookings"], summary["cancellations"]), (3, 1))
        self.assertEqual(list(response.context["invoices"]), [invoice])
        self.assertContains(response, invoice.number)
        self.assertNotContains(response, other_invoice.number)
        self.assertNotContains(response, "Sam")
        self.assertContains(
            response, reverse("dashboard:invoice_pdf", args=[invoice.pk])
        )
        self.assertContains(
            response, reverse("dashboard:booking_detail", args=[expired.pk])
        )
        self.assertContains(response, 'href="mailto:alex@example.com"')

    def test_no_email_address_in_the_url(self):
        order = self.order("alex@example.com")
        response = self.get()
        url = reverse("dashboard:customer_detail", args=[order.pk])
        self.assertEqual(url, f"/dashboard/klanten/{order.pk}/")
        self.assertContains(response, f'href="{url}"')
        self.assertNotIn("@", url)

    def test_customer_with_only_an_expired_booking(self):
        order = self.order("weg@example.com", status=OrderStatus.EXPIRED)
        response = self.client.get(
            reverse("dashboard:customer_detail", args=[order.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["summary"])
        self.assertContains(response, "nog niet")

    def test_unknown_booking_is_404(self):
        response = self.client.get(reverse("dashboard:customer_detail", args=[999]))
        self.assertEqual(response.status_code, 404)

    def test_customer_orders_uses_the_same_key(self):
        order = self.order(" MiXeD@Example.com ")
        self.order("mixed@example.com")
        self.order("mixed@example.org")
        self.assertEqual(customer_orders(order).count(), 2)


class CustomerExportTests(CustomerTestCase):
    def export(self, **params):
        response = self.client.get(reverse("dashboard:customer_export"), params)
        self.assertEqual(response.status_code, 200)
        content = b"".join(response.streaming_content).decode("utf-8")
        self.assertTrue(content.startswith("﻿"))
        rows = list(csv.reader(io.StringIO(content[1:]), delimiter=";"))
        header, *records = rows
        return response, [dict(zip(header, record)) for record in records]

    def test_csv_for_excel(self):
        self.order("ALEX@example.com", start_in=-40, status=OrderStatus.CANCELLED)
        newest = self.order(
            "alex@example.com", start_in=-3, first_name="Zoë", last_name="Ñúñez"
        )

        response, rows = self.export()

        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertEqual(
            response["Content-Disposition"],
            'attachment; filename="klanten-2027-03-01.csv"',
        )
        self.assertEqual(
            rows,
            [
                {
                    "Naam": "Zoë Ñúñez",
                    "E-mail": "alex@example.com",
                    "Boekingen": "2",
                    "Annuleringen": "1",
                    "Totaal besteed": "200,00",
                    "Laatste verblijf": "2027-02-26",
                    "Laatste boeking": str(newest.pk),
                }
            ],
        )

    def test_follows_search_and_sort_and_has_more_than_one_chunk(self):
        for number in range(510):
            self.order(f"klant{number:03d}@example.com", nights=1)
        self.order("ander@example.org")

        _, rows = self.export(q="example.com", sort="email")

        self.assertEqual(len(rows), 510)
        self.assertEqual(rows[0]["E-mail"], "klant000@example.com")
        self.assertEqual(rows[-1]["E-mail"], "klant509@example.com")
        self.assertTrue(all(row["Naam"] for row in rows))

    def test_formulas_are_neutralised(self):
        self.order("=cmd@example.com", first_name="=HYPERLINK(1)", last_name="x")

        _, rows = self.export()

        self.assertEqual(rows[0]["Naam"], "'=HYPERLINK(1) x")
        self.assertEqual(rows[0]["E-mail"], "'=cmd@example.com")

    def test_export_is_logged_without_personal_data(self):
        self.order("jan@example.com", first_name="Jan")

        with self.assertLogs("maykinbooking.dashboard.views", "INFO") as logs:
            self.export(q="jan@example.com", sort="-besteed")

        self.assertEqual(
            logs.output,
            [
                "INFO:maykinbooking.dashboard.views:customer export "
                f"user={self.staff.pk} rows=1 filters=q=*,sort=-besteed"
            ],
        )
