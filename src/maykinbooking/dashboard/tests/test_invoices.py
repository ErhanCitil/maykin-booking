"""
The invoice list of the dashboard (US5.3): columns, filters, the PDF of every
invoice and credit note, and the CSV export.
"""
import csv
import io
from datetime import date
from decimal import Decimal

from django.db import connection, transaction
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from maykinbooking.accounts.tests.factories import ManagerUserFactory
from maykinbooking.base.models import OrderStatus
from maykinbooking.base.tests.factories import (
    CityFactory,
    HotelFactory,
    OrderFactory,
    RoomFactory,
)
from maykinbooking.billing.models import Invoice, InvoiceKind
from maykinbooking.billing.services.invoicing import issue_credit_note, issue_invoice
from maykinbooking.billing.tests.utils import TempMediaMixin

from ..views import csv_amount, csv_cell

SELLER = {
    "INVOICE_SELLER_NAME": "Maykin Booking B.V.",
    "INVOICE_SELLER_ADDRESS": "Kingsfordweg 151\\n1043 GR Amsterdam",
    "INVOICE_SELLER_KVK": "12345678",
    "INVOICE_SELLER_VAT_ID": "NL123456789B01",
}


class InvoiceTestCase(TempMediaMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = ManagerUserFactory()
        cls.hotel = HotelFactory(
            city=CityFactory(name="Amsterdam"), name="Hotel aan de Gracht"
        )
        cls.other_hotel = HotelFactory(
            city=CityFactory(name="Utrecht"), name="Hotel Dom"
        )
        # 121 a night: 100 without 21% VAT.
        cls.room = RoomFactory(hotel=cls.hotel, price=Decimal("121.00"))
        cls.other_room = RoomFactory(hotel=cls.other_hotel, price=Decimal("60.50"))

    def setUp(self):
        self.client.force_login(self.staff)

    def invoice(self, issued, *, room=None, start=date(2026, 3, 10), nights=2, **guest):
        room = room or self.room
        order = OrderFactory(
            hotel=room.hotel,
            room=room,
            start_date=start,
            end_date=date.fromordinal(start.toordinal() + nights),
            **guest,
        )
        with transaction.atomic():
            return issue_invoice(order, today=issued)

    def credit(self, invoice, issued):
        with transaction.atomic():
            type(invoice.order).objects.filter(pk=invoice.order_id).update(
                status=OrderStatus.CANCELLED
            )
            return issue_credit_note(invoice, today=issued)

    def get(self, **params):
        return self.client.get(reverse("dashboard:invoice_list"), params)

    def numbers(self, response):
        return [invoice.number for invoice in response.context["invoices"]]


class InvoiceListTests(InvoiceTestCase):
    def test_columns(self):
        invoice = self.invoice(
            date(2026, 3, 1),
            first_name="Alex",
            last_name="de Landgraaf",
            email="alex@example.com",
        )
        credit_note = self.credit(invoice, date(2026, 3, 5))

        response = self.get()

        self.assertEqual(self.numbers(response), [credit_note.number, invoice.number])
        self.assertContains(response, "2 documenten")
        self.assertContains(response, invoice.number)
        self.assertContains(response, "1 mrt 2026")
        self.assertContains(response, "Factuur")
        self.assertContains(response, "Creditnota")
        self.assertContains(response, f"bij {invoice.number}")
        self.assertContains(response, "Alex de Landgraaf")
        self.assertContains(response, "alex@example.com")
        self.assertContains(response, "Hotel aan de Gracht")
        # One notation: a credit note with a real minus sign.
        self.assertContains(response, "\u2212 € 242,00")
        self.assertNotContains(response, "€ -242")
        self.assertContains(response, ">€ 242,00<")
        self.assertContains(response, "incl. 21% btw")
        self.assertContains(
            response, reverse("dashboard:booking_detail", args=[invoice.order_id])
        )
        self.assertContains(
            response, reverse("dashboard:invoice_pdf", args=[invoice.pk])
        )
        self.assertContains(
            response, reverse("dashboard:invoice_pdf", args=[credit_note.pk])
        )
        # Together they are 0.
        self.assertEqual(
            response.context["totals"],
            {"excl": Decimal("0.00"), "vat": Decimal("0.00"), "incl": Decimal("0.00")},
        )

    def test_test_badge(self):
        test_invoice = self.invoice(date(2026, 3, 1))
        with override_settings(**SELLER):
            real_invoice = self.invoice(date(2026, 3, 2))
        self.assertTrue(test_invoice.is_test)
        self.assertFalse(real_invoice.is_test)

        response = self.get()

        self.assertContains(response, ">Test</span>", count=1)
        self.assertContains(response, "Testfactuur: de bedrijfsgegevens")

    def test_filter_on_period_kind_and_search(self):
        march = self.invoice(
            date(2026, 3, 31), first_name="Maart", email="maart@example.com"
        )
        april = self.invoice(date(2026, 4, 1), room=self.other_room, first_name="April")
        credit_note = self.credit(march, date(2026, 4, 2))

        self.assertEqual(
            self.numbers(self.get(van="2026-04-01")), [credit_note.number, april.number]
        )
        self.assertEqual(self.numbers(self.get(tot="2026-03-31")), [march.number])
        self.assertEqual(
            self.numbers(self.get(van="2026-04-01", tot="2026-04-01")), [april.number]
        )
        self.assertEqual(
            self.numbers(self.get(soort=InvoiceKind.CREDIT_NOTE)), [credit_note.number]
        )
        self.assertEqual(
            self.numbers(self.get(soort=InvoiceKind.INVOICE)),
            [april.number, march.number],
        )
        # The number, the customer name or the e-mail address.
        self.assertEqual(self.numbers(self.get(q=april.number)), [april.number])
        self.assertEqual(
            self.numbers(self.get(q="maart")), [credit_note.number, march.number]
        )
        self.assertEqual(
            self.numbers(self.get(q="MAART@example")),
            [credit_note.number, march.number],
        )
        self.assertEqual(
            self.numbers(self.get(q="maart", soort=InvoiceKind.INVOICE)), [march.number]
        )
        self.assertEqual(self.numbers(self.get(q="niemand")), [])

    def test_invalid_filters_show_an_error_and_are_ignored(self):
        invoice = self.invoice(date(2026, 3, 1))

        response = self.get(van="gisteren", soort="bon")
        self.assertEqual(self.numbers(response), [invoice.number])
        self.assertIn("van", response.context["form"].errors)
        self.assertIn("soort", response.context["form"].errors)
        self.assertContains(response, 'aria-describedby="id_van_errors"')

        response = self.get(van="2026-04-01", tot="2026-03-01")
        self.assertEqual(
            response.context["form"].errors["tot"],
            ["De einddatum mag niet voor de begindatum liggen."],
        )

    def test_pagination_keeps_the_filters(self):
        for day in range(1, 28):
            self.invoice(date(2026, 3, day))

        response = self.get(soort=InvoiceKind.INVOICE)

        self.assertEqual(len(response.context["invoices"]), 25)
        self.assertContains(response, "?soort=invoice&amp;page=2")
        self.assertContains(
            response, f'{reverse("dashboard:invoice_export")}?soort=invoice"'
        )

    def test_number_of_queries_does_not_grow_with_the_invoices(self):
        def count():
            with CaptureQueriesContext(connection) as context:
                self.assertEqual(self.get().status_code, 200)
            return len(context.captured_queries)

        first = self.invoice(date(2026, 3, 1))
        self.credit(first, date(2026, 3, 2))
        few = count()
        for day in range(3, 13):
            self.credit(self.invoice(date(2026, 3, day)), date(2026, 3, day))
        self.assertEqual(count(), few)


class InvoicePdfTests(InvoiceTestCase):
    def test_download_the_invoice_and_the_credit_note(self):
        invoice = self.invoice(date(2026, 3, 1))
        credit_note = self.credit(invoice, date(2026, 3, 2))

        for document, filename in (
            (invoice, f"factuur_{invoice.number}.pdf"),
            (credit_note, f"creditnota_{credit_note.number}.pdf"),
        ):
            with self.subTest(number=document.number):
                with self.assertLogs("maykinbooking.dashboard.views", "INFO") as logs:
                    response = self.client.get(
                        reverse("dashboard:invoice_pdf", args=[document.pk])
                    )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"], "application/pdf")
                self.assertEqual(
                    response["Content-Disposition"],
                    f'attachment; filename="{filename}"',
                )
                self.assertTrue(response.content.startswith(b"%PDF"))
                self.assertIn("no-store", response["Cache-Control"])
                self.assertEqual(
                    logs.output,
                    [
                        "INFO:maykinbooking.dashboard.views:invoice pdf "
                        f"user={self.staff.pk} invoice={document.pk}"
                    ],
                )

    def test_the_stored_pdf_is_handed_out_again(self):
        invoice = self.invoice(date(2026, 3, 1))
        first = self.client.get(reverse("dashboard:invoice_pdf", args=[invoice.pk]))
        invoice = Invoice.objects.get(pk=invoice.pk)
        self.assertTrue(invoice.pdf)

        second = self.client.get(reverse("dashboard:invoice_pdf", args=[invoice.pk]))

        self.assertEqual(first.content, second.content)

    def test_unknown_invoice_is_404(self):
        response = self.client.get(reverse("dashboard:invoice_pdf", args=[999]))
        self.assertEqual(response.status_code, 404)


class InvoiceExportTests(InvoiceTestCase):
    def test_only_our_own_amounts_skip_the_formula_check(self):
        self.assertEqual(csv_cell(csv_amount(Decimal("-1.5"))), "-1,50")
        self.assertEqual(csv_cell("-1,50"), "'-1,50")

    def export(self, **params):
        response = self.client.get(reverse("dashboard:invoice_export"), params)
        self.assertEqual(response.status_code, 200)
        content = b"".join(response.streaming_content).decode("utf-8")
        return response, content

    def rows(self, content):
        self.assertTrue(content.startswith("﻿"))
        rows = list(csv.reader(io.StringIO(content[1:]), delimiter=";"))
        header, *records = rows
        return [dict(zip(header, record)) for record in records]

    def test_csv_for_excel(self):
        invoice = self.invoice(
            date(2026, 3, 1),
            first_name="Zoë",
            last_name="Ñúñez",
            email="zoe@example.com",
        )
        credit_note = self.credit(invoice, date(2026, 3, 2))

        response, content = self.export()

        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertRegex(
            response["Content-Disposition"],
            r'^attachment; filename="facturen-\d{4}-\d{2}-\d{2}\.csv"$',
        )
        self.assertTrue(content.startswith("﻿Nummer;Datum;Soort;"))
        credit_row, invoice_row = self.rows(content)
        self.assertEqual(
            invoice_row,
            {
                "Nummer": invoice.number,
                "Datum": "2026-03-01",
                "Soort": "Factuur",
                "Bij factuur": "",
                "Boeking": str(invoice.order_id),
                "Klant": "Zoë Ñúñez",
                "E-mail": "zoe@example.com",
                "Hotel": "Hotel aan de Gracht",
                "Kamer": invoice.room_type,
                "Inchecken": "2026-03-10",
                "Uitchecken": "2026-03-12",
                "Nachten": "2",
                "Btw-land": "NL",
                "Btw-tarief": "21,00",
                "Excl. btw": "200,00",
                "Btw": "42,00",
                "Incl. btw": "242,00",
                "Testfactuur": "ja",
            },
        )
        self.assertEqual(credit_row["Soort"], "Creditnota")
        self.assertEqual(credit_row["Bij factuur"], invoice.number)
        # Our own amounts stay numbers for Excel, also when negative.
        self.assertEqual(credit_row["Incl. btw"], "-242,00")
        self.assertEqual(credit_row["Excl. btw"], "-200,00")
        self.assertEqual(credit_row["Btw"], "-42,00")
        self.assertEqual(credit_note.number, credit_row["Nummer"])

    def test_follows_the_filters(self):
        march = self.invoice(date(2026, 3, 1))
        self.invoice(date(2026, 4, 1))
        self.credit(march, date(2026, 4, 2))

        _, content = self.export(
            soort=InvoiceKind.INVOICE, van="2026-03-01", tot="2026-03-31"
        )

        self.assertEqual([row["Nummer"] for row in self.rows(content)], [march.number])

    def test_formulas_are_neutralised(self):
        hotel = HotelFactory(city=CityFactory(), name="-2+3")
        self.invoice(
            date(2026, 3, 1),
            room=RoomFactory(hotel=hotel, price=Decimal("100.00")),
            first_name='=HYPERLINK("https://evil.example")',
            last_name="x",
            email="+31@example.com",
        )

        _, content = self.export()

        (row,) = self.rows(content)
        self.assertEqual(row["Klant"], """'=HYPERLINK("https://evil.example") x""")
        self.assertEqual(row["E-mail"], "'+31@example.com")
        self.assertEqual(row["Hotel"], "'-2+3")
        self.assertNotIn(";=HYPERLINK", content)

    def test_export_is_logged_without_personal_data(self):
        self.invoice(date(2026, 3, 1), first_name="Jan", email="jan@example.com")

        with self.assertLogs("maykinbooking.dashboard.views", "INFO") as logs:
            _, content = self.export(q="jan@example.com", soort="invoice")

        self.assertEqual(
            logs.output,
            [
                "INFO:maykinbooking.dashboard.views:invoice export "
                f"user={self.staff.pk} rows=1 filters=q=*,soort=invoice"
            ],
        )
        self.assertNotIn("jan", logs.output[0])
