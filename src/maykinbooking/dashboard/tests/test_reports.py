"""
Occupancy and revenue per hotel (E3.4): the formula, blocks, a stay over the
end of a month, and the revenue net of credit notes.
"""
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.test import TestCase
from django.urls import reverse

from freezegun import freeze_time

from maykinbooking.accounts.tests.factories import ManagerUserFactory
from maykinbooking.base.models import Order, OrderStatus
from maykinbooking.base.tests.factories import (
    CityFactory,
    HotelFactory,
    OrderFactory,
    RoomBlockFactory,
    RoomFactory,
)
from maykinbooking.billing.services.invoicing import issue_credit_note, issue_invoice

from ..reports import current_month, hotel_report, report_total, revenue_total

MARCH = (date(2026, 3, 1), date(2026, 4, 1))
APRIL = (date(2026, 4, 1), date(2026, 5, 1))


class ReportTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.hotel = HotelFactory(
            city=CityFactory(name="Amsterdam"), name="Hotel Gracht"
        )
        # 121 and 60,50 a night: 100 and 50 without 21% VAT.
        cls.double = RoomFactory(hotel=cls.hotel, price=Decimal("121.00"), inventory=2)
        cls.single = RoomFactory(hotel=cls.hotel, price=Decimal("60.50"), inventory=1)
        cls.empty_hotel = HotelFactory(
            city=CityFactory(name="Utrecht"), name="Hotel Leeg"
        )

    def order(self, room, start, end, *, invoice=True, **fields):
        order = OrderFactory(
            hotel=room.hotel, room=room, start_date=start, end_date=end, **fields
        )
        if invoice:
            with transaction.atomic():
                issue_invoice(order, today=start)
        return order

    def cancel(self, order, on):
        with transaction.atomic():
            Order.objects.filter(pk=order.pk).update(status=OrderStatus.CANCELLED)
            issue_credit_note(order.invoices.get(), today=on)

    def row(self, period, hotel=None):
        rows = {row.hotel_id: row for row in hotel_report(*period)}
        return rows[(hotel or self.hotel).pk]


class FormulaTests(ReportTestCase):
    def test_capacity_blocked_booked_occupancy_and_revenue(self):
        # Blocked: 5 of the 2 doubles (at most 2 a night) on 5 and 6 March, and
        # 1 more on 6 and 7 March: 2 + 2 + 1 = 5 room nights.
        RoomBlockFactory(
            room=self.double,
            start_date=date(2026, 3, 5),
            end_date=date(2026, 3, 7),
            quantity=5,
        )
        RoomBlockFactory(
            room=self.double,
            start_date=date(2026, 3, 6),
            end_date=date(2026, 3, 8),
            quantity=1,
        )
        # From February: only the night of 1 March counts.
        RoomBlockFactory(
            room=self.single, start_date=date(2026, 2, 27), end_date=date(2026, 3, 2)
        )
        self.order(self.double, date(2026, 3, 10), date(2026, 3, 12))
        # From before the invoices: booked, no revenue.
        self.order(self.double, date(2026, 3, 15), date(2026, 3, 16), invoice=False)
        # Not booked: cancelled, expired, waiting for payment.
        self.order(
            self.single,
            date(2026, 3, 3),
            date(2026, 3, 4),
            invoice=False,
            status=OrderStatus.CANCELLED,
        )
        self.order(
            self.single,
            date(2026, 3, 3),
            date(2026, 3, 4),
            invoice=False,
            status=OrderStatus.EXPIRED,
        )
        self.order(
            self.single,
            date(2026, 3, 3),
            date(2026, 3, 4),
            invoice=False,
            status=OrderStatus.PENDING_PAYMENT,
        )

        row = self.row(MARCH)

        self.assertEqual(row.capacity, 3 * 31)
        self.assertEqual(row.blocked, 6)
        self.assertEqual(row.available, 87)
        self.assertEqual(row.booked, 3)
        # 3 / 87 = 3,448..%
        self.assertEqual(row.occupancy, Decimal("3.4"))
        self.assertEqual(row.revenue, Decimal("200.00"))

    def test_hotel_without_rooms(self):
        row = self.row(MARCH, self.empty_hotel)
        self.assertEqual((row.capacity, row.blocked, row.booked), (0, 0, 0))
        self.assertIsNone(row.occupancy)
        self.assertEqual(row.revenue, Decimal("0.00"))

    def test_everything_blocked(self):
        for room in (self.double, self.single):
            RoomBlockFactory(
                room=room, start_date=MARCH[0], end_date=MARCH[1], quantity=9
            )
        row = self.row(MARCH)
        self.assertEqual(row.blocked, row.capacity)
        self.assertIsNone(row.occupancy)

    def test_a_stay_over_the_end_of_the_month(self):
        # 2 nights of 121 with a discount code of 24,20: 217,80, 180 without VAT,
        # spread pro rata: 90 for each night.
        self.order(
            self.double,
            date(2026, 3, 31),
            date(2026, 4, 2),
            total_price=Decimal("217.80"),
            code_discount=Decimal("24.20"),
            discount_code="KORTING",
        )

        march, april = self.row(MARCH), self.row(APRIL)

        self.assertEqual((march.booked, april.booked), (1, 1))
        self.assertEqual(
            (march.revenue, april.revenue), (Decimal("90.00"), Decimal("90.00"))
        )
        self.assertEqual(revenue_total(MARCH[0], APRIL[1]).revenue, Decimal("180.00"))

    def test_revenue_is_net_after_a_credit_note(self):
        kept = self.order(self.single, date(2026, 3, 10), date(2026, 3, 12))
        cancelled = self.order(self.double, date(2026, 3, 20), date(2026, 3, 22))
        # The credit note is issued in April, but its nights are in March.
        self.cancel(cancelled, date(2026, 4, 2))

        row = self.row(MARCH)

        self.assertEqual(row.booked, 2)
        self.assertEqual(row.revenue, Decimal("100.00"))
        self.assertEqual(self.row(APRIL).revenue, Decimal("0.00"))
        self.assertEqual(kept.invoices.get().total_excl, Decimal("100.00"))

    def test_total_row(self):
        self.order(self.double, date(2026, 3, 10), date(2026, 3, 12))
        other = HotelFactory(city=CityFactory(name="Delft"), name="Hotel Delft")
        room = RoomFactory(hotel=other, price=Decimal("60.50"), inventory=4)
        self.order(room, date(2026, 3, 1), date(2026, 3, 5))

        total = report_total(hotel_report(*MARCH), name="Totaal")

        self.assertEqual(total.capacity, (3 + 4) * 31)
        self.assertEqual(total.booked, 6)
        self.assertEqual(total.revenue, Decimal("400.00"))
        self.assertEqual(total.occupancy, Decimal("2.8"))

    def test_only_the_chosen_hotels(self):
        rows = hotel_report(*MARCH, hotel_ids=[self.empty_hotel.pk])
        self.assertEqual([row.name for row in rows], ["Hotel Leeg"])

    def test_invoices_without_vat_rate_are_not_revenue(self):
        # No VAT rate for Belgium: the amount without VAT is not known.
        belgian = HotelFactory(
            city=CityFactory(name="Brussel"), name="Hotel Brussel", country="BE"
        )
        room = RoomFactory(hotel=belgian, price=Decimal("121.00"), inventory=1)
        untaxed = self.order(room, date(2026, 3, 10), date(2026, 3, 12))
        kept = self.order(room, date(2026, 3, 20), date(2026, 3, 21))
        self.cancel(untaxed, date(2026, 3, 11))
        # A test invoice (the seller details are missing) with a rate counts.
        taxed = self.order(self.double, date(2026, 3, 10), date(2026, 3, 12))
        self.assertIsNone(kept.invoices.get().vat_rate)
        self.assertTrue(taxed.invoices.get().is_test)

        rows = {row.hotel_id: row for row in hotel_report(*MARCH)}

        row = rows[belgian.pk]
        self.assertEqual(row.booked, 1)
        self.assertEqual(row.revenue, Decimal("0.00"))
        # The invoice of the cancelled stay and the kept one; a credit note is
        # part of its booking, it is not counted as another invoice.
        self.assertEqual(row.untaxed_invoices, 2)
        self.assertEqual(row.untaxed_incl, Decimal("121.00"))
        self.assertEqual(rows[self.hotel.pk].revenue, Decimal("200.00"))
        self.assertEqual(rows[self.hotel.pk].untaxed_invoices, 0)

        total = report_total(rows.values())
        self.assertEqual(total.revenue, Decimal("200.00"))
        self.assertEqual(
            (total.untaxed_invoices, total.untaxed_incl), (2, Decimal("121.00"))
        )
        month = revenue_total(*MARCH)
        self.assertEqual(
            (month.revenue, month.untaxed_invoices, month.untaxed_incl),
            (Decimal("200.00"), 2, Decimal("121.00")),
        )

    def test_five_queries_whatever_the_number_of_hotels(self):
        for number in range(5):
            hotel = HotelFactory(city=CityFactory(), name=f"Hotel {number}")
            room = RoomFactory(hotel=hotel, price=Decimal("121.00"))
            RoomBlockFactory(
                room=room, start_date=date(2026, 3, 1), end_date=date(2026, 3, 3)
            )
            self.order(room, date(2026, 3, 10), date(2026, 3, 12))
        with self.assertNumQueries(5):
            rows = hotel_report(*MARCH)
        self.assertEqual(len(rows), 7)

    def test_current_month(self):
        self.assertEqual(
            current_month(date(2026, 2, 14)), (date(2026, 2, 1), date(2026, 3, 1))
        )
        self.assertEqual(
            current_month(date(2026, 12, 31)), (date(2026, 12, 1), date(2027, 1, 1))
        )


@freeze_time("2026-03-15T10:00:00Z")
class ReportViewTests(ReportTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(ManagerUserFactory())

    def get(self, **params):
        return self.client.get(reverse("dashboard:report"), params)

    def test_this_month_by_default(self):
        self.order(self.double, date(2026, 3, 10), date(2026, 3, 12))

        response = self.get()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["start"], date(2026, 3, 1))
        self.assertEqual(response.context["last"], date(2026, 3, 31))
        self.assertContains(response, "1 maart 2026 t/m 31 maart 2026 · 31 nachten")
        self.assertContains(response, "Hotel Gracht")
        self.assertContains(response, "2,2%")
        self.assertContains(response, "€ 200,00")
        # The total row, when there is more than one hotel.
        self.assertContains(response, "Totaal")

    def test_choose_the_period_and_hotel(self):
        self.order(self.double, date(2026, 3, 31), date(2026, 4, 2))

        response = self.get(van="2026-04-01", tot="2026-04-10", hotel=self.hotel.pk)

        self.assertEqual(response.context["nights"], 10)
        (row,) = response.context["rows"]
        self.assertEqual((row.capacity, row.booked), (30, 1))
        self.assertEqual(row.revenue, Decimal("100.00"))
        # Only in the choice of hotels, not as a row.
        self.assertContains(response, "Hotel Leeg", count=1)
        self.assertContains(response, f'<option value="{self.hotel.pk}" selected>')

    def test_untaxed_invoices_are_mentioned(self):
        belgian = HotelFactory(
            city=CityFactory(name="Brussel"), name="Hotel Brussel", country="BE"
        )
        room = RoomFactory(hotel=belgian, price=Decimal("121.00"), inventory=1)
        self.order(room, date(2026, 3, 10), date(2026, 3, 12))
        self.order(self.double, date(2026, 3, 10), date(2026, 3, 12))

        response = self.get()

        # On the row of the hotel and on the total row.
        self.assertContains(
            response,
            "1 factuur zonder btw-tarief niet meegeteld (€ 242,00 incl.)",
            count=2,
        )
        self.assertContains(response, ">€ 200,00<")

        response = self.client.get(reverse("dashboard:home"))
        self.assertEqual(response.context["month_revenue"], Decimal("200.00"))
        self.assertEqual(response.context["month_untaxed_invoices"], 1)
        self.assertEqual(response.context["month_untaxed_incl"], Decimal("242.00"))
        self.assertContains(
            response, "1 factuur zonder btw-tarief niet meegeteld (€ 242,00 incl.)"
        )

    def test_one_date_gives_the_rest_of_its_month(self):
        response = self.get(van="2026-02-10")
        self.assertEqual(
            (response.context["start"], response.context["last"]),
            (date(2026, 2, 10), date(2026, 2, 28)),
        )

    def test_invalid_period(self):
        response = self.get(van="2026-04-10", tot="2026-04-01")
        self.assertEqual(
            response.context["form"].errors["tot"],
            ["De einddatum mag niet voor de begindatum liggen."],
        )
        response = self.get(van="2025-01-01", tot="2026-12-31")
        self.assertIn("hoogstens 366 dagen", response.context["form"].errors["tot"][0])
        response = self.get(van="morgen")
        self.assertEqual(response.context["start"], date(2026, 3, 1))
        self.assertContains(response, 'aria-describedby="id_van_errors"')

    def test_month_links_keep_the_hotel(self):
        response = self.get(van="2026-01-01", tot="2026-01-31", hotel=self.hotel.pk)
        links = [link["query"] for link in response.context["month_links"]]
        self.assertEqual(
            links,
            [
                f"van=2025-12-01&hotel={self.hotel.pk}",
                f"van=2026-03-01&hotel={self.hotel.pk}",
                f"van=2026-02-01&hotel={self.hotel.pk}",
            ],
        )
