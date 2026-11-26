"""
The invoice and credit note PDF (``templates/billing/invoice_pdf.html``) as the
guest and the accountant read it: the snapshot of the invoice only, negative
amounts on a credit note, and the watermark of a test invoice.

The HTML is taken from ``render_invoice_pdf`` (WeasyPrint mocked), so it is the
context of the real rendering; ``RenderInvoicePdfTests`` makes real PDFs.
"""
from datetime import date
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings

import weasyprint
from freezegun import freeze_time

from maykinbooking.base.models import Order
from maykinbooking.base.tests.factories import HotelFactory, RoomFactory
from maykinbooking.bookings.services.booking import Actor, cancel_booking
from maykinbooking.utils.pdf import data_url_fetcher

from ..models import DiscountKind, InvoiceKind, PricingSettings
from ..pdf import render_invoice_pdf
from .factories import DiscountCodeFactory, SeasonPeriodFactory
from .utils import GUEST, TempMediaMixin, book

SELLER = {
    "INVOICE_SELLER_NAME": "Maykin Booking B.V.",
    "INVOICE_SELLER_ADDRESS": "Kingsfordweg 151\\n1043 GR Amsterdam",
    "INVOICE_SELLER_KVK": "12345678",
    "INVOICE_SELLER_VAT_ID": "NL001234567B01",
}
# Thursday 26 November 2026.
NOW = "2026-11-26T10:00:00Z"
# Thursday 10 to Sunday 13 December 2026: Thursday € 100, Friday (weekend
# +20%) € 120, Saturday (weekend +20%, Kerstmarkt +50%) € 180; subtotal € 400,
# 10% off for a long stay (€ 40) and WELKOM (€ 5): € 355,00. 21% VAT over the
# total: € 293,39 + € 61,61.
ARRIVAL = date(2026, 12, 10)
DEPARTURE = date(2026, 12, 13)


@freeze_time(NOW)
@override_settings(**SELLER)
class InvoicePdfTestCase(TempMediaMixin, TestCase):
    def setUp(self):
        self.hotel = HotelFactory(
            is_available=True,
            name="Hotel de Gracht",
            address="Keizersgracht 1",
            city__name="Amsterdam",
        )
        self.room = RoomFactory(
            hotel=self.hotel,
            room_type="Double",
            price=Decimal("100.00"),
            inventory=2,
            is_available=True,
        )
        PricingSettings.load()
        PricingSettings.objects.update(
            weekend_percent=Decimal("20"),
            long_stay_min_nights=3,
            long_stay_percent=Decimal("10"),
        )
        SeasonPeriodFactory(
            name="Kerstmarkt",
            start_date=date(2026, 12, 12),
            end_date=date(2026, 12, 31),
            percent=Decimal("50"),
        )
        DiscountCodeFactory(code="WELKOM", kind=DiscountKind.FIXED, value=Decimal("5"))

    def book(self, **kwargs):
        kwargs.setdefault("discount_code", "WELKOM")
        return book(self.room, ARRIVAL, DEPARTURE, **kwargs)

    def invoice(self, **kwargs):
        return self.book(**kwargs).invoices.get(kind=InvoiceKind.INVOICE)

    def credit_note(self, **kwargs):
        order = self.book(**kwargs)
        cancel_booking(order.pk, actor=Actor.guest())
        return order.invoices.get(kind=InvoiceKind.CREDIT_NOTE)

    def render_html(self, invoice):
        """The HTML that ``render_invoice_pdf`` gives to WeasyPrint."""
        with mock.patch("maykinbooking.billing.pdf.weasyprint.HTML") as html:
            html.return_value.write_pdf.return_value = b"%PDF-1.7"
            render_invoice_pdf(invoice, list(invoice.lines.order_by("position")))
        self.assertIs(html.call_args.kwargs["url_fetcher"], data_url_fetcher)
        return html.call_args.kwargs["string"]

    def assertInOrder(self, html, *parts):
        """Every part is in ``html``, each one after the one before it."""
        position = 0
        for part in parts:
            found = html.find(part, position)
            self.assertNotEqual(found, -1, f"{part!r} not after {parts}")
            position = found + len(part)

    def assertNoWatermark(self, html):
        self.assertNotIn("TESTFACTUUR", html)
        self.assertNotIn("Testfactuur", html)
        self.assertNotIn('class="watermark"', html)

    def assertWatermark(self, html):
        self.assertIn(
            '<div class="watermark" aria-hidden="true"><span>TESTFACTUUR</span></div>',
            html,
        )
        self.assertIn("<strong>Testfactuur – niet geldig</strong>", html)
        # The footer of every page.
        self.assertIn("TESTFACTUUR – NIET GELDIG · ", html)


class InvoiceTemplateTests(InvoicePdfTestCase):
    def test_heading_number_and_date(self):
        invoice = self.invoice()
        html = self.render_html(invoice)

        self.assertEqual(invoice.number, "2026-00001")
        self.assertIn("<title>Factuur 2026-00001</title>", html)
        self.assertIn("<h1>Factuur</h1>", html)
        self.assertIn("Factuurnummer 2026-00001 · factuurdatum 26 november 2026", html)
        self.assertNotIn("Creditnota", html)
        self.assertNotIn("Bij factuur", html)

    def test_seller_and_customer(self):
        html = self.render_html(self.invoice())

        for expected in (
            "Maykin Booking B.V.",
            "Kingsfordweg 151<br>1043 GR Amsterdam",
            "KvK-nummer: 12345678",
            "Btw-id: NL001234567B01",
            "Alex de Landgraaf",
            "Keizersgracht 117<br>1015CJ Amsterdam<br>Nederland",
            "alex@example.com",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, html)
        self.assertInOrder(html, "Verkoper", "Maykin Booking B.V.", "Klant", "Alex")

    def test_description_of_the_stay(self):
        html = self.render_html(self.invoice())

        for expected in (
            "Hotel de Gracht",
            "Keizersgracht 1, Amsterdam",
            ">Double<",
            "10 dec 2026 – 13 dec 2026",
            '<span class="value">3</span>',
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, html)

    def test_a_line_per_night_then_the_discounts(self):
        html = self.render_html(self.invoice())

        self.assertInOrder(
            html,
            "10-12-2026",
            "Overnachting donderdag 10 december 2026",
            "€ 100,00",
            "11-12-2026",
            "Overnachting vrijdag 11 december 2026 (weekend +20%)",
            "€ 120,00",
            "12-12-2026",
            "Overnachting zaterdag 12 december 2026 (weekend +20%, Kerstmarkt +50%)",
            "€ 180,00",
            "Korting lang verblijf (10%)",
            "− € 40,00",
            "Kortingscode WELKOM",
            "− € 5,00",
        )
        self.assertIn('<tr class="discount">', html)
        self.assertIn("Bedrag incl. btw", html)

    def test_totals(self):
        invoice = self.invoice()
        html = self.render_html(invoice)

        self.assertEqual(
            (invoice.total_excl, invoice.vat_amount, invoice.total_incl),
            (Decimal("293.39"), Decimal("61.61"), Decimal("355.00")),
        )
        self.assertInOrder(
            html,
            "Totaal excl. btw",
            "€ 293,39",
            "Btw 21%",
            "€ 61,61",
            "Totaal incl. btw",
            "€ 355,00",
        )

    def test_payment_note_and_tourist_tax(self):
        html = self.render_html(self.invoice())

        self.assertIn("<strong>Te betalen in het hotel.</strong>", html)
        self.assertIn(
            "Toeristenbelasting is niet inbegrepen; die wordt in het hotel afgerekend.",
            html,
        )

    def test_no_watermark_on_a_real_invoice(self):
        invoice = self.invoice()
        html = self.render_html(invoice)

        self.assertFalse(invoice.is_test)
        self.assertNoWatermark(html)
        self.assertIn('content: "Factuur 2026-00001";', html)

    def test_watermark_on_a_test_invoice(self):
        with override_settings(INVOICE_SELLER_KVK="", INVOICE_SELLER_VAT_ID=""):
            invoice = self.invoice()
        html = self.render_html(invoice)

        self.assertTrue(invoice.is_test)
        self.assertWatermark(html)
        self.assertIn(
            'content: "TESTFACTUUR – NIET GELDIG · Factuur 2026-00001";', html
        )
        self.assertIn("KvK-nummer: —", html)
        self.assertIn("Btw-id: —", html)

    def test_unknown_vat_rate(self):
        self.hotel.country = "BE"
        self.hotel.save()
        invoice = self.invoice()
        html = self.render_html(invoice)

        self.assertTrue(invoice.is_test)
        self.assertWatermark(html)
        self.assertInOrder(
            html,
            "Totaal excl. btw",
            "€ 355,00",
            "Btw (tarief onbekend)",
            "€ 0,00",
            "Totaal incl. btw",
            "€ 355,00",
        )

    def test_only_the_snapshot(self):
        invoice = self.invoice()
        # Everything changes after the invoice was issued.
        self.hotel.name = "Hotel Nieuw"
        self.hotel.address = "Damrak 1"
        self.hotel.save()
        self.room.price = Decimal("999.00")
        self.room.save()
        Order.objects.filter(pk=invoice.order_id).update(
            first_name="Kim",
            last_name="Jansen",
            email="kim@example.com",
            address="Damrak 2",
            total_price=Decimal("1.00"),
        )
        invoice.order.refresh_from_db()
        with override_settings(
            INVOICE_SELLER_NAME="Andere B.V.", INVOICE_SELLER_KVK="87654321"
        ):
            html = self.render_html(invoice)

        for old in ("Hotel de Gracht", "Alex de Landgraaf", "Maykin Booking B.V."):
            with self.subTest(old=old):
                self.assertIn(old, html)
        for new in ("Hotel Nieuw", "Damrak", "Kim", "999,00", "Andere B.V.", "8765"):
            with self.subTest(new=new):
                self.assertNotIn(new, html)
        self.assertIn("€ 355,00", html)

    def test_text_is_escaped(self):
        guest = {**GUEST, "last_name": "de <b>Landgraaf</b>"}
        html = self.render_html(self.invoice(guest=guest))

        self.assertIn("Alex de &lt;b&gt;Landgraaf&lt;/b&gt;", html)
        self.assertNotIn("<b>Landgraaf", html)

    def test_loads_nothing_external(self):
        html = self.render_html(self.invoice())

        for external in ("<script", "<link", "<img", "src=", "@import", "url(", "http"):
            with self.subTest(external=external):
                self.assertNotIn(external, html)


class CreditNoteTemplateTests(InvoicePdfTestCase):
    def test_heading_refers_to_the_invoice(self):
        credit_note = self.credit_note()
        html = self.render_html(credit_note)

        self.assertEqual(credit_note.number, "2026-00002")
        self.assertIn("<title>Creditnota 2026-00002</title>", html)
        self.assertIn("<h1>Creditnota</h1>", html)
        self.assertIn("Creditnotanummer 2026-00002 · datum 26 november 2026", html)
        self.assertIn("Bij factuur 2026-00001 van 26 november 2026", html)
        self.assertIn('content: "Creditnota 2026-00002";', html)

    def test_amounts_are_negative(self):
        html = self.render_html(self.credit_note())

        self.assertInOrder(
            html,
            "Overnachting donderdag 10 december 2026",
            "− € 100,00",
            "Overnachting vrijdag 11 december 2026 (weekend +20%)",
            "− € 120,00",
            "Overnachting zaterdag 12 december 2026",
            "− € 180,00",
            # A discount is credited too: it is positive on the credit note.
            "Korting lang verblijf (10%)",
            "€ 40,00",
            "Kortingscode WELKOM",
            "€ 5,00",
            "Totaal excl. btw",
            "− € 293,39",
            "Btw 21%",
            "− € 61,61",
            "Totaal incl. btw",
            "− € 355,00",
        )
        self.assertNotIn("− € 40,00", html)
        # Never a minus sign glued to the amount.
        self.assertNotIn("€ -", html)
        self.assertNotIn("-€", html)

    def test_same_snapshot_as_the_invoice(self):
        html = self.render_html(self.credit_note())

        for expected in (
            "Maykin Booking B.V.",
            "KvK-nummer: 12345678",
            "Alex de Landgraaf",
            "Keizersgracht 117<br>1015CJ Amsterdam<br>Nederland",
            "Hotel de Gracht",
            "10 dec 2026 – 13 dec 2026",
            "<strong>Er hoeft niets betaald te worden.</strong>",
            "Toeristenbelasting is niet inbegrepen; die wordt in het hotel afgerekend.",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, html)

    def test_no_watermark_on_a_real_credit_note(self):
        credit_note = self.credit_note()

        self.assertFalse(credit_note.is_test)
        self.assertNoWatermark(self.render_html(credit_note))

    def test_watermark_on_a_test_credit_note(self):
        with override_settings(INVOICE_SELLER_NAME=""):
            credit_note = self.credit_note()
        html = self.render_html(credit_note)

        self.assertTrue(credit_note.is_test)
        self.assertWatermark(html)
        self.assertIn(
            'content: "TESTFACTUUR – NIET GELDIG · Creditnota 2026-00002";', html
        )
        self.assertIn("Bij factuur 2026-00001 van 26 november 2026", html)

    def test_no_negative_zero(self):
        # Without a VAT rate the VAT is 0, credited as -0.00.
        self.hotel.country = "BE"
        self.hotel.save()
        credit_note = self.credit_note()
        html = self.render_html(credit_note)

        self.assertEqual(credit_note.vat_amount, Decimal("0"))
        self.assertInOrder(html, "Btw (tarief onbekend)", "€ 0,00", "Totaal incl. btw")
        self.assertNotIn("− € 0,00", html)
        self.assertNotIn("-0,00", html)


class RenderInvoicePdfTests(InvoicePdfTestCase):
    def test_pdf_bytes(self):
        with override_settings(INVOICE_SELLER_NAME=""):
            test_credit_note = self.credit_note()
        credit_note = self.credit_note()
        documents = [
            ("invoice", credit_note.credited_invoice),
            ("credit note", credit_note),
            ("test invoice", test_credit_note.credited_invoice),
            ("test credit note", test_credit_note),
        ]
        for name, invoice in documents:
            with self.subTest(name), mock.patch(
                "maykinbooking.billing.pdf.data_url_fetcher", wraps=data_url_fetcher
            ) as fetcher, mock.patch(
                "maykinbooking.billing.pdf.weasyprint.HTML", wraps=weasyprint.HTML
            ) as html, self.assertNoLogs(
                "weasyprint", "WARNING"
            ):
                pdf = render_invoice_pdf(
                    invoice, list(invoice.lines.order_by("position"))
                )
                self.assertIsInstance(pdf, bytes)
                self.assertTrue(pdf.startswith(b"%PDF"))
                self.assertIs(html.call_args.kwargs["url_fetcher"], fetcher)
                # Nothing to fetch: no files, no URLs, not even data: URLs.
                fetcher.assert_not_called()
