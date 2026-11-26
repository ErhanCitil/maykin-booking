"""
The booking mails and the booking PDF as the guest reads them.

The templates are rendered directly with ``booking_context``, the context that
``send_booking_email`` and ``render_order_pdf`` use, in the language of the order.
A failure here points at a template, not at sending (see test_emails.py).

The money in them comes from snapshots: the nights (with their surcharges), the
discounts on the order, the payment, the refund, the invoice and credit note.
"""
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

from django.template.loader import render_to_string
from django.test import TestCase, override_settings
from django.utils import translation

from freezegun import freeze_time

from maykinbooking.base.models import OrderStatus, PaymentStatus
from maykinbooking.base.tests.factories import OrderFactory
from maykinbooking.billing.models import PaymentState, RefundReason, RefundState
from maykinbooking.billing.services.invoicing import issue_credit_note, issue_invoice
from maykinbooking.billing.tests.factories import PaymentFactory, RefundFactory

from ..emails import booking_context
from ..pdf import render_order_pdf

# Thursday 26 November 2026, 11:00 in Amsterdam.
NOW = "2026-11-26 10:00:00"
# Friday 4 December to Monday 7 December 2026: three nights.
ARRIVAL = date(2026, 12, 4)
DEPARTURE = date(2026, 12, 7)
NIGHT_PRICES = [Decimal("75.50"), Decimal("75.50"), Decimal("92.00")]
# Thursday 26 November 2026, 10:30 in Amsterdam.
PAID_AT = datetime(2026, 11, 26, 9, 30, tzinfo=dt_timezone.utc)
# Friday 27 November 2026, 13:00 in Amsterdam.
REFUNDED_AT = datetime(2026, 11, 27, 12, 0, tzinfo=dt_timezone.utc)

CONFIRMATION = "emails/booking_confirmation"
CANCELLATION = "emails/booking_cancellation"
REFUND = "emails/payment_refund"

# (base price, weekend %, season %, season name, price) of the three nights:
# Friday weekend, Saturday weekend and season, Sunday season only.
SURCHARGED_NIGHTS = [
    ("100.00", "20", "0", "", "120.00"),
    ("100.00", "20", "50", "Kerst & <Markt>", "180.00"),
    ("100.00", "0", "50", "Kerst & <Markt>", "150.00"),
]


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
            zipcode="1015CJ",
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

    def surcharge(self, order, nights=SURCHARGED_NIGHTS):
        """Give the nights of ``order`` weekend and season surcharges."""
        for night, values in zip(order.nights.order_by("date"), nights):
            base, weekend, season, name, price = values
            night.base_price = Decimal(base)
            night.weekend_percent = Decimal(weekend)
            night.season_percent = Decimal(season)
            night.season_name = name
            night.price = Decimal(price)
            night.save()
        order.total_price = sum(Decimal(values[-1]) for values in nights)
        order.save()
        return order

    def discount(self, order):
        """10% off for a long stay and WELKOM (€ 5) off the € 243,00 of the nights."""
        order.length_discount_percent = Decimal("10")
        order.length_discount = Decimal("24.30")
        order.discount_code = "WELKOM"
        order.code_discount = Decimal("5.00")
        order.total_price -= order.length_discount + order.code_discount
        order.save()
        return order

    def pay(self, order, paid_at=PAID_AT):
        """The order was paid online (iDEAL); returns the payment."""
        order.payment_status = PaymentStatus.PAID
        order.save()
        return PaymentFactory(
            order=order,
            status=PaymentState.PAID,
            amount=order.total_price,
            method="ideal",
            paid_at=paid_at,
        )

    def refund(self, payment, **fields):
        return RefundFactory(payment=payment, amount=payment.amount, **fields)

    def cancel(self, order):
        order.status = OrderStatus.CANCELLED
        order.save()
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

    def assertLoadsNothingExternal(self, html):
        for external in ("<script", "<link", "<img", "src=", "@import", "url("):
            with self.subTest(external=external):
                self.assertNotIn(external, html)


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
                self.assertIn("Prijsopbouw", version)
                # Neither the current nor the original room price.
                self.assertNotIn("129,00", version)
                self.assertNotIn("80,00", version)
                # No discounts: no subtotal.
                self.assertNotIn("Subtotaal", version)
                self.assertNotIn("basisprijs", version)
        self.assertIn("- vrijdag 4 december: € 75,50\n", text)
        self.assertIn("- zondag 6 december: € 92,00\n", text)
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
                self.assertIn("PDF in de bijlage", version)
                self.assertIn("hulp@example.com", version)

    def test_at_the_hotel(self):
        _, text, html = self.render_mail(CONFIRMATION, self.make_order())

        for version in (text, html):
            with self.subTest(version=version[:20]):
                self.assertIn("Je betaalt in het hotel bij aankomst.", version)
                self.assertNotIn("Betaald", version)
        self.assertIn("Totaal: € 243,00\nJe betaalt in het hotel bij aankomst.\n", text)

    def test_paid_online(self):
        order = self.make_order()
        self.pay(order)
        subject, text, html = self.render_mail(CONFIRMATION, order)

        self.assertEqual(subject, "Bevestiging boeking #1234 – Hotel & Spa <Amsterdam>")
        for version in (text, html):
            with self.subTest(version=version[:20]):
                self.assertIn("Betaald via iDEAL op 26 november 2026.", version)
                self.assertNotIn("Je betaalt in het hotel", version)
                self.assertIn("€ 243,00", version)
        self.assertIn(
            "Totaal: € 243,00\nBetaald via iDEAL op 26 november 2026.\n", text
        )

    def test_paid_online_without_a_payment_date(self):
        order = self.make_order()
        self.pay(order, paid_at=None)
        _, text, html = self.render_mail(CONFIRMATION, order)

        for version in (text, html):
            with self.subTest(version=version[:20]):
                self.assertIn("Betaald via iDEAL.", version)
                self.assertNotIn("Betaald via iDEAL op", version)

    def test_paid_at_is_shown_in_amsterdam(self):
        order = self.make_order()
        # 23:30 UTC on 30 November is 00:30 on 1 December in Amsterdam.
        self.pay(order, paid_at=datetime(2026, 11, 30, 23, 30, tzinfo=dt_timezone.utc))
        _, text, _ = self.render_mail(CONFIRMATION, order)

        self.assertIn("Betaald via iDEAL op 1 december 2026.", text)

    def test_discounts(self):
        order = self.discount(self.make_order())
        _, text, html = self.render_mail(CONFIRMATION, order)

        self.assertIn(
            "- zondag 6 december: € 92,00\n"
            "\n"
            "Subtotaal: € 243,00\n"
            "Korting lang verblijf (10%): − € 24,30\n"
            "Kortingscode WELKOM: − € 5,00\n"
            "Totaal: € 213,70\n",
            text,
        )
        self.assertIn("Subtotaal", html)
        self.assertIn("€ 243,00", html)
        self.assertIn("Korting lang verblijf (10%)", html)
        self.assertIn("− € 24,30", html)
        self.assertIn("Kortingscode WELKOM", html)
        self.assertIn("− € 5,00", html)
        self.assertIn("€ 213,70", html)
        # The total row comes after the discounts.
        self.assertLess(html.index("− € 5,00"), html.index("€ 213,70"))

    def test_weekend_and_season_surcharges(self):
        order = self.surcharge(self.make_order())
        _, text, html = self.render_mail(CONFIRMATION, order)

        self.assertIn(
            "- vrijdag 4 december: € 120,00 (basisprijs € 100,00, weekend +20%)\n"
            "- zaterdag 5 december: € 180,00 (basisprijs € 100,00, weekend +20%, "
            "Kerst & <Markt> +50%)\n"
            "- zondag 6 december: € 150,00 (basisprijs € 100,00, Kerst & <Markt> +50%)\n",
            text,
        )
        self.assertIn("Totaal: € 450,00", text)
        self.assertRegex(html, r"basisprijs € 100,00, weekend \+20%\s*</span>")
        self.assertIn(
            "basisprijs € 100,00, weekend +20%, Kerst &amp; &lt;Markt&gt; +50%", html
        )
        self.assertIn("basisprijs € 100,00, Kerst &amp; &lt;Markt&gt; +50%", html)
        self.assertNotIn("<Markt>", html)
        self.assertIn("€ 450,00", html)

    def test_season_without_a_name_and_a_broken_percentage(self):
        order = self.surcharge(
            self.make_order(),
            [
                ("100.00", "12.50", "0", "", "112.50"),
                ("100.00", "0", "25", "", "125.00"),
                ("100.00", "0", "0", "", "100.00"),
            ],
        )
        _, text, _ = self.render_mail(CONFIRMATION, order)

        # The same label as the checkout, the invoice and "Mijn boeking".
        self.assertIn("€ 112,50 (basisprijs € 100,00, weekend +12,5%)", text)
        self.assertIn("€ 125,00 (basisprijs € 100,00, seizoen +25%)", text)
        self.assertIn("- zondag 6 december: € 100,00\n", text)

    def test_the_invoice_is_attached(self):
        order = self.make_order()
        issue_invoice(order)
        _, text, html = self.render_mail(CONFIRMATION, order)

        for version in (text, html):
            with self.subTest(version=version[:20]):
                self.assertIn("De bevestiging zit ook als PDF in de bijlage.", version)
                self.assertIn(
                    "De factuur zit als PDF in de bijlage (factuurnummer 2026-00001).",
                    version,
                )

    def test_no_invoice_sentence_for_orders_from_before_invoicing(self):
        _, text, html = self.render_mail(CONFIRMATION, self.make_order())

        for version in (text, html):
            with self.subTest(version=version[:20]):
                self.assertNotIn("factuur", version.lower())

    def test_paid_with_discounts_and_surcharges(self):
        order = self.surcharge(self.make_order())
        order.length_discount_percent = Decimal("10")
        order.length_discount = Decimal("45.00")
        order.total_price -= order.length_discount
        order.save()
        self.pay(order)
        issue_invoice(order)
        _, text, html = self.render_mail(CONFIRMATION, order)

        self.assertIn(
            "Subtotaal: € 450,00\n"
            "Korting lang verblijf (10%): − € 45,00\n"
            "Totaal: € 405,00\n"
            "Betaald via iDEAL op 26 november 2026.\n",
            text,
        )
        for expected in (
            "basisprijs € 100,00, weekend +20%",
            "− € 45,00",
            "€ 405,00",
            "Betaald via iDEAL op 26 november 2026.",
            "De factuur zit als PDF in de bijlage (factuurnummer 2026-00001).",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, html)

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
        order = self.discount(self.surcharge(self.make_order()))
        self.pay(order)
        _, _, html = self.render_mail(CONFIRMATION, order)

        self.assertLoadsNothingExternal(html)


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
                self.assertIn(url, version)
                self.assertIn("hulp@example.com", version)
                self.assertNotIn("Kosteloos annuleren", version)

    def test_at_the_hotel_nothing_to_pay(self):
        _, text, html = self.render_mail(CANCELLATION, self.order)

        for version in (text, html):
            with self.subTest(version=version[:20]):
                self.assertIn("Je hoeft niets te betalen.", version)
                self.assertIn("Er wordt niets in rekening gebracht.", version)
                self.assertNotIn("teruggestort", version)
                self.assertNotIn("€", version)

    def test_paid_is_refunded(self):
        payment = self.pay(self.order)
        self.refund(payment, status=RefundState.REQUESTED)
        subject, text, html = self.render_mail(CANCELLATION, self.order)

        self.assertEqual(subject, "Je boeking #1234 is geannuleerd")
        for version in (text, html):
            with self.subTest(version=version[:20]):
                self.assertIn(
                    "€ 243,00 wordt teruggestort op je rekening (1–3 werkdagen).",
                    version,
                )
                self.assertIn(
                    "Je krijgt een e-mail zodra we het bedrag hebben teruggestort.",
                    version,
                )
                self.assertNotIn("Je hoeft niets te betalen", version)
                self.assertNotIn("Er wordt niets in rekening gebracht", version)

    def test_paid_before_the_refund_is_made(self):
        # The refund failed at the provider (closed): it is made again later.
        payment = self.pay(self.order)
        self.refund(payment, status=RefundState.FAILED)
        _, text, html = self.render_mail(CANCELLATION, self.order)

        for version in (text, html):
            with self.subTest(version=version[:20]):
                self.assertIn(
                    "€ 243,00 wordt teruggestort op je rekening (1–3 werkdagen).",
                    version,
                )
                self.assertNotIn("Je hoeft niets te betalen", version)

    def test_refund_already_done(self):
        # The mail is sent again by staff after the money went back.
        payment = self.pay(self.order)
        self.order.payment_status = PaymentStatus.REFUNDED
        self.order.save()
        self.refund(payment, status=RefundState.REFUNDED, refunded_at=REFUNDED_AT)
        _, text, html = self.render_mail(CANCELLATION, self.order)

        for version in (text, html):
            with self.subTest(version=version[:20]):
                self.assertIn(
                    "€ 243,00 is op 27 november 2026 teruggestort op je rekening.",
                    version,
                )
                self.assertNotIn("wordt teruggestort", version)
                self.assertNotIn("Je krijgt een e-mail", version)

    def test_credit_note_is_attached(self):
        order = self.make_order(id=1235)
        invoice = issue_invoice(order)
        issue_credit_note(invoice)
        self.cancel(order)
        _, text, html = self.render_mail(CANCELLATION, order)

        for version in (text, html):
            with self.subTest(version=version[:20]):
                self.assertIn(
                    "De creditnota zit als PDF in de bijlage "
                    "(creditnotanummer 2026-00002).",
                    version,
                )
                self.assertIn("Je hoeft niets te betalen.", version)

    def test_no_credit_note_sentence_without_one(self):
        _, text, html = self.render_mail(CANCELLATION, self.order)

        for version in (text, html):
            with self.subTest(version=version[:20]):
                self.assertNotIn("creditnota", version)

    def test_html_is_escaped_and_loads_nothing_external(self):
        payment = self.pay(self.order)
        self.refund(payment)
        _, text, html = self.render_mail(CANCELLATION, self.order)

        self.assertIn("Hotel &amp; Spa &lt;Amsterdam&gt;", html)
        self.assertNotIn("<Amsterdam>", html)
        self.assertIn("Hotel & Spa <Amsterdam>", text)
        self.assertLoadsNothingExternal(html)


class RefundMailTests(TemplateTestCase):
    def refunded(
        self, reason=RefundReason.CANCELLATION, status=OrderStatus.CANCELLED, **fields
    ):
        """An order whose payment went back (the mail is sent after that)."""
        order = self.make_order(status=status, **fields)
        payment = self.pay(order)
        order.payment_status = PaymentStatus.REFUNDED
        order.save()
        self.refund(
            payment,
            reason=reason,
            status=RefundState.REFUNDED,
            refunded_at=REFUNDED_AT,
        )
        return order

    def test_subject_is_one_line(self):
        for pk, reason in enumerate(RefundReason.values, start=1234):
            with self.subTest(reason=reason):
                subject, _, _ = self.render_mail(REFUND, self.refunded(reason, id=pk))
                self.assertEqual(subject, f"Terugbetaling boeking #{pk}")

    def test_cancellation(self):
        order = self.refunded()
        url = self.my_booking_url(order)
        _, text, html = self.render_mail(REFUND, order)

        self.assertIn(
            "Je boeking #1234 bij Hotel & Spa <Amsterdam> is geannuleerd. "
            "We storten het betaalde bedrag terug.",
            text,
        )
        self.assertIn(
            "Je boeking #1234 bij Hotel &amp; Spa &lt;Amsterdam&gt; is geannuleerd.",
            html,
        )
        self.assertIn(
            "Boeking: #1234\n"
            "Hotel: Hotel & Spa <Amsterdam>\n"
            "Bedrag: € 243,00\n"
            "Teruggestort op: 27 november 2026\n",
            text,
        )
        self.assertIn(f'href="{url}"', html)
        for version in (text, html):
            with self.subTest(version=version[:20]):
                self.assertIn("Beste Alex,", version)
                self.assertIn("#1234", version)
                self.assertNotIn("1.234", version)
                self.assertIn("€ 243,00", version)
                self.assertIn("27 november 2026", version)
                self.assertIn(
                    "Het bedrag staat binnen 1 tot 3 werkdagen op je rekening.",
                    version,
                )
                self.assertIn(url, version)
                self.assertIn("hulp@example.com", version)
                self.assertNotIn("verlopen", version)
                self.assertNotIn("twee keer", version)

    def test_late_payment(self):
        order = self.refunded(RefundReason.LATE_PAYMENT, status=OrderStatus.EXPIRED)
        subject, text, html = self.render_mail(REFUND, order)

        self.assertEqual(subject, "Terugbetaling boeking #1234")
        for version in (text, html):
            with self.subTest(version=version[:20]):
                self.assertIn(
                    "Je betaling kwam binnen nadat de reservering was verlopen; "
                    "we storten het bedrag terug.",
                    version,
                )
                self.assertIn("is daardoor niet doorgegaan", version)
                self.assertIn("€ 243,00", version)
                self.assertIn("27 november 2026", version)
                self.assertNotIn("is geannuleerd", version)

    def test_duplicate_payment(self):
        order = self.refunded(RefundReason.DUPLICATE, status=OrderStatus.CONFIRMED)
        _, text, html = self.render_mail(REFUND, order)

        for version in (text, html):
            with self.subTest(version=version[:20]):
                self.assertIn("Je hebt boeking #1234 twee keer betaald.", version)
                self.assertIn("je boeking blijft gewoon staan", version)
                self.assertNotIn("is geannuleerd", version)

    def test_html_loads_nothing_external(self):
        _, _, html = self.render_mail(REFUND, self.refunded())

        self.assertLoadsNothingExternal(html)


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
        self.assertNotIn("Subtotaal", html)

    def test_booking_details(self):
        order = self.make_order()
        html = self.render("order_pdf.html", order)

        self.assertIn("#1234", html)
        self.assertNotIn("1.234", html)
        self.assertIn("1015CJ Amsterdam, Nederland", html)
        self.assertIn("Kosteloos annuleren kan tot en met woensdag 2 december.", html)
        self.assertIn(self.my_booking_url(order), html)
        self.assertIn("hulp@example.com", html)
        self.assertNotIn("GEANNULEERD", html)

    def test_surcharges_and_discounts(self):
        order = self.surcharge(self.make_order())
        order.length_discount_percent = Decimal("10")
        order.length_discount = Decimal("45.00")
        order.discount_code = "WELKOM"
        order.code_discount = Decimal("5.00")
        order.total_price -= order.length_discount + order.code_discount
        order.save()
        html = self.render("order_pdf.html", order)

        self.assertRegex(html, r"basisprijs € 100,00, weekend \+20%\s*</span>")
        for expected in (
            "basisprijs € 100,00, weekend +20%, Kerst &amp; &lt;Markt&gt; +50%",
            "basisprijs € 100,00, Kerst &amp; &lt;Markt&gt; +50%",
            "€ 120,00",
            "€ 180,00",
            "€ 150,00",
            "Subtotaal",
            "€ 450,00",
            "Korting lang verblijf (10%)",
            "− € 45,00",
            "Kortingscode WELKOM",
            "− € 5,00",
            "€ 400,00",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, html)
        self.assertNotIn("<Markt>", html)
        self.assertLess(html.index("Subtotaal"), html.index("− € 45,00"))
        self.assertLess(html.index("− € 5,00"), html.index("€ 400,00"))

    def test_at_the_hotel(self):
        html = self.render("order_pdf.html", self.make_order())

        self.assertIn("Je betaalt het totaalbedrag in het hotel bij aankomst.", html)
        self.assertNotIn("Betaald via iDEAL", html)
        self.assertNotIn("teruggestort", html)

    def test_paid(self):
        order = self.make_order()
        self.pay(order)
        html = self.render("order_pdf.html", order)

        self.assertIn("Betaald via iDEAL op 26 november 2026.", html)
        self.assertNotIn("in het hotel bij aankomst", html)
        self.assertNotIn("teruggestort", html)

    def test_cancelled_booking_is_marked(self):
        html = self.render(
            "order_pdf.html", self.make_order(status=OrderStatus.CANCELLED)
        )

        self.assertIn("GEANNULEERD", html)
        self.assertIn("Geannuleerde boeking", html)
        self.assertIn("Je hoeft niets te betalen.", html)
        self.assertNotIn("Kosteloos annuleren", html)
        self.assertNotIn("in het hotel bij aankomst", html)

    def test_cancelled_and_being_refunded(self):
        order = self.make_order(status=OrderStatus.CANCELLED)
        self.refund(self.pay(order))
        html = self.render("order_pdf.html", order)

        self.assertIn("Het betaalde bedrag wordt teruggestort.", html)
        self.assertIn("Betaald via iDEAL op 26 november 2026.", html)
        self.assertIn(
            "€ 243,00 wordt teruggestort op je rekening (1–3 werkdagen).", html
        )
        self.assertNotIn("Je hoeft niets te betalen", html)

    def test_cancelled_and_refunded(self):
        order = self.make_order(status=OrderStatus.CANCELLED)
        payment = self.pay(order)
        order.payment_status = PaymentStatus.REFUNDED
        order.save()
        self.refund(payment, status=RefundState.REFUNDED, refunded_at=REFUNDED_AT)
        html = self.render("order_pdf.html", order)

        self.assertIn("Het betaalde bedrag is teruggestort.", html)
        self.assertIn(
            "€ 243,00 is op 27 november 2026 teruggestort op je rekening.", html
        )
        self.assertNotIn("wordt teruggestort", html)

    def test_render_order_pdf(self):
        statuses = (OrderStatus.CONFIRMED, OrderStatus.CANCELLED)
        for pk, status in enumerate(statuses, start=1234):
            with self.subTest(status=status):
                pdf = render_order_pdf(self.make_order(id=pk, status=status))
                self.assertIsInstance(pdf, bytes)
                self.assertTrue(pdf.startswith(b"%PDF"))

    def test_render_order_pdf_with_money_details(self):
        order = self.discount(self.surcharge(self.make_order()))
        self.refund(self.pay(order))
        self.cancel(order)

        pdf = render_order_pdf(order)

        self.assertTrue(pdf.startswith(b"%PDF"))
