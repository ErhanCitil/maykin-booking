"""
The booking mails and the booking PDF as the guest reads them.

The templates are rendered directly with ``booking_context``, the context that
``send_booking_email`` and ``render_order_pdf`` use, in the language of the order.
A failure here points at a template, not at sending (see test_emails.py).
"""
from datetime import date, timedelta
from decimal import Decimal

from django.template.loader import render_to_string
from django.test import TestCase, override_settings
from django.utils import translation

from freezegun import freeze_time

from maykinbooking.base.models import OrderStatus
from maykinbooking.base.tests.factories import OrderFactory

from ..emails import booking_context
from ..pdf import render_order_pdf

# Thursday 26 November 2026, 11:00 in Amsterdam.
NOW = "2026-11-26 10:00:00"
# Friday 4 December to Monday 7 December 2026: three nights.
ARRIVAL = date(2026, 12, 4)
DEPARTURE = date(2026, 12, 7)
NIGHT_PRICES = [Decimal("75.50"), Decimal("75.50"), Decimal("92.00")]

CONFIRMATION = "emails/booking_confirmation"
CANCELLATION = "emails/booking_cancellation"


@override_settings(
    SITE_URL="https://booking.example.com",
    BOOKING_CONTACT_EMAIL="hulp@example.com",
)
class TemplateTestCase(TestCase):
    def setUp(self):
        freezer = freeze_time(NOW)
        freezer.start()
        self.addCleanup(freezer.stop)

    def make_order(self, start=ARRIVAL, end=DEPARTURE, prices=None, **fields):
        """
        An order (#1234 by default) whose nights were booked at other prices than
        the current price of the room: the snapshot must win.
        """
        prices = NIGHT_PRICES if prices is None else prices
        fields.setdefault("id", 1234)
        order = OrderFactory(
            start_date=start,
            end_date=end,
            first_name="Alex",
            hotel__name="Hotel & Spa <Amsterdam>",
            hotel__address="Keizersgracht 117",
            hotel__city__name="Amsterdam",
            room__room_type="Double",
            room__price=Decimal("80.00"),
            language="nl",
            **fields,
        )
        for night, price in zip(order.nights.order_by("date"), prices):
            night.price = price
            night.save()
        order.total_price = sum(prices)
        order.save()
        # The room got more expensive after the booking.
        order.room.price = Decimal("129.00")
        order.room.save()
        return order

    def render(self, template, order):
        with translation.override(order.language):
            return render_to_string(template, booking_context(order))

    def render_mail(self, name, order):
        """The subject, text and HTML version of a mail."""
        return (
            self.render(f"{name}_subject.txt", order),
            self.render(f"{name}.txt", order),
            self.render(f"{name}.html", order),
        )

    def my_booking_url(self, order):
        with translation.override(order.language):
            return booking_context(order)["my_booking_url"]


class ConfirmationMailTests(TemplateTestCase):
    def test_subject_is_one_unescaped_line(self):
        subject, _, _ = self.render_mail(CONFIRMATION, self.make_order())

        self.assertEqual(subject, "Bevestiging boeking #1234 – Hotel & Spa <Amsterdam>")
        self.assertNotIn("\n", subject)

    def test_prices_are_the_snapshot_of_the_nights(self):
        _, text, html = self.render_mail(CONFIRMATION, self.make_order())

        for version in (text, html):
            with self.subTest(version=version[:20]):
                self.assertEqual(version.count("€ 75,50"), 2)
                self.assertIn("€ 92,00", version)
                self.assertIn("€ 243,00", version)
                # Neither the current nor the original room price.
                self.assertNotIn("129,00", version)
                self.assertNotIn("80,00", version)
        self.assertIn("- vrijdag 4 december: € 75,50", text)
        self.assertIn("- zondag 6 december: € 92,00", text)
        self.assertIn("Totaal: € 243,00", text)
        self.assertIn("Totaal (3 nachten)", html)

    def test_links_to_my_booking_in_both_versions(self):
        order = self.make_order()
        url = self.my_booking_url(order)
        _, text, html = self.render_mail(CONFIRMATION, order)

        self.assertEqual(url, f"https://booking.example.com/boeking/{order.token}/")
        self.assertIn(url, text)
        self.assertIn(f'href="{url}"', html)
        self.assertIn("Bekijk je boeking", text)
        self.assertIn("Bekijk je boeking", html)

    def test_booking_number_has_no_thousand_separator(self):
        subject, text, html = self.render_mail(CONFIRMATION, self.make_order())

        for version in (subject, text, html):
            with self.subTest(version=version[:20]):
                self.assertIn("#1234", version)
                self.assertNotIn("1.234", version)

    def test_shows_the_stay(self):
        _, text, html = self.render_mail(CONFIRMATION, self.make_order())

        for version in (text, html):
            with self.subTest(version=version[:20]):
                self.assertIn("Beste Alex,", version)
                self.assertIn("Je boeking staat vast", version)
                self.assertIn("Keizersgracht 117, Amsterdam", version)
                self.assertIn("Double", version)
                self.assertIn("vrijdag 4 december 2026", version)
                self.assertIn("maandag 7 december 2026", version)
                self.assertIn("3 nachten", version)
                self.assertIn("Je betaalt in het hotel bij aankomst.", version)
                self.assertIn("PDF in de bijlage", version)
                self.assertIn("hulp@example.com", version)

    def test_free_cancellation_until_the_last_day(self):
        # Arrival Friday 4 December: free cancellation through Wednesday 2 December
        # (the deadline is Thursday 3 December 00:00).
        _, text, html = self.render_mail(CONFIRMATION, self.make_order())

        for version in (text, html):
            with self.subTest(version=version[:20]):
                self.assertIn(
                    "Kosteloos annuleren kan tot en met woensdag 2 december.", version
                )
                self.assertNotIn("niet meer", version)

    def test_no_free_cancellation_after_the_deadline(self):
        tomorrow = date(2026, 11, 27)
        order = self.make_order(
            start=tomorrow,
            end=tomorrow + timedelta(days=1),
            prices=[Decimal("75.50")],
        )
        _, text, html = self.render_mail(CONFIRMATION, order)

        for version in (text, html):
            with self.subTest(version=version[:20]):
                self.assertNotIn("Kosteloos annuleren kan tot", version)
                self.assertIn(
                    "Kosteloos annuleren kan voor deze boeking niet meer.", version
                )
                self.assertIn("hulp@example.com", version)
                self.assertIn("1 nacht", version)
                self.assertNotIn("1 nachten", version)

    def test_html_is_escaped_and_text_is_not(self):
        _, text, html = self.render_mail(CONFIRMATION, self.make_order())

        self.assertIn("Hotel &amp; Spa &lt;Amsterdam&gt;", html)
        self.assertNotIn("<Amsterdam>", html)
        self.assertIn("Hotel & Spa <Amsterdam>", text)
        self.assertNotIn("&amp;", text)

    def test_html_loads_nothing_external(self):
        _, _, html = self.render_mail(CONFIRMATION, self.make_order())

        for external in ("<script", "<link", "<img", "src=", "@import", "url("):
            with self.subTest(external=external):
                self.assertNotIn(external, html)


class CancellationMailTests(TemplateTestCase):
    def setUp(self):
        super().setUp()
        self.order = self.make_order(status=OrderStatus.CANCELLED)

    def test_subject_is_one_line(self):
        subject, _, _ = self.render_mail(CANCELLATION, self.order)

        self.assertEqual(subject, "Je boeking #1234 is geannuleerd")
        self.assertNotIn("\n", subject)

    def test_content(self):
        url = self.my_booking_url(self.order)
        _, text, html = self.render_mail(CANCELLATION, self.order)

        self.assertIn(f'href="{url}"', html)
        for version in (text, html):
            with self.subTest(version=version[:20]):
                self.assertIn("Beste Alex,", version)
                self.assertIn("is geannuleerd", version)
                self.assertIn("#1234", version)
                self.assertNotIn("1.234", version)
                self.assertIn("vrijdag 4 december 2026", version)
                self.assertIn("maandag 7 december 2026", version)
                self.assertIn("Je hoeft niets te betalen.", version)
                self.assertIn(url, version)
                self.assertIn("hulp@example.com", version)
                self.assertNotIn("Kosteloos annuleren", version)
                self.assertNotIn("€", version)


class OrderPdfTests(TemplateTestCase):
    def test_price_lines_are_the_snapshot_of_the_nights(self):
        html = self.render("order_pdf.html", self.make_order())

        self.assertEqual(html.count("€ 75,50"), 2)
        self.assertIn("€ 92,00", html)
        self.assertIn("€ 243,00", html)
        self.assertIn("vrijdag 4 december 2026", html)
        self.assertIn("zondag 6 december 2026", html)
        self.assertNotIn("129,00", html)
        self.assertNotIn("80,00", html)

    def test_booking_details(self):
        order = self.make_order()
        html = self.render("order_pdf.html", order)

        self.assertIn("#1234", html)
        self.assertNotIn("1.234", html)
        self.assertIn("Kosteloos annuleren kan tot en met woensdag 2 december.", html)
        self.assertIn(self.my_booking_url(order), html)
        self.assertIn("hulp@example.com", html)
        self.assertNotIn("GEANNULEERD", html)

    def test_cancelled_booking_is_marked(self):
        html = self.render(
            "order_pdf.html", self.make_order(status=OrderStatus.CANCELLED)
        )

        self.assertIn("GEANNULEERD", html)
        self.assertIn("Geannuleerde boeking", html)
        self.assertNotIn("Kosteloos annuleren", html)

    def test_render_order_pdf(self):
        statuses = (OrderStatus.CONFIRMED, OrderStatus.CANCELLED)
        for pk, status in enumerate(statuses, start=1234):
            with self.subTest(status=status):
                pdf = render_order_pdf(self.make_order(id=pk, status=status))
                self.assertIsInstance(pdf, bytes)
                self.assertTrue(pdf.startswith(b"%PDF"))
