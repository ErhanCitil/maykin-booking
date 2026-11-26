"""
Downloading an invoice or credit note from "Mijn boeking"
(``bookings:invoice``): only with the token of its own order.
"""
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from freezegun import freeze_time

from maykinbooking.base.tests.factories import HotelFactory, OrderFactory, RoomFactory
from maykinbooking.billing.models import InvoiceKind
from maykinbooking.billing.tests.utils import (
    TempMediaMixin,
    book,
    commit_hooks,
    in_days,
)

from ..services.booking import Actor, cancel_booking


class InvoiceDownloadTestCase(TempMediaMixin, TestCase):
    def setUp(self):
        # The rate limits count in the cache, which all tests share.
        cache.clear()
        self.room = RoomFactory(
            hotel=HotelFactory(is_available=True), price=Decimal("100"), inventory=5
        )
        with commit_hooks():
            self.order = book(self.room, in_days(10), in_days(12))
        self.invoice = self.order.invoices.get()

    def url(self, invoice=None, order=None):
        return reverse(
            "bookings:invoice",
            args=[(order or self.order).token, (invoice or self.invoice).pk],
        )

    def assert_private(self, response):
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(response["X-Robots-Tag"], "noindex")
        self.assertEqual(response["Referrer-Policy"], "no-referrer")


class InvoiceDownloadTests(InvoiceDownloadTestCase):
    def test_url(self):
        self.assertEqual(
            self.url(), f"/boeking/{self.order.token}/factuur/{self.invoice.pk}/"
        )

    def test_download(self):
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(
            response["Content-Disposition"],
            f'attachment; filename="factuur_{self.invoice.number}.pdf"',
        )
        self.assertTrue(response.content.startswith(b"%PDF"))
        self.assert_private(response)

    def test_the_stored_file_is_served(self):
        self.invoice.refresh_from_db()
        self.assertTrue(self.invoice.pdf)
        with self.invoice.pdf.open("rb") as stored:
            content = stored.read()
        self.assertEqual(self.client.get(self.url()).content, content)

    @mock.patch(
        "maykinbooking.bookings.views.invoice_pdf_bytes", return_value=b"%PDF-1.7"
    )
    def test_uses_the_same_file_as_the_mail(self, invoice_pdf_bytes):
        response = self.client.get(self.url())
        self.assertEqual(response.content, b"%PDF-1.7")
        invoice_pdf_bytes.assert_called_once_with(self.invoice)

    def test_credit_note(self):
        with commit_hooks():
            cancel_booking(self.order.pk, actor=Actor.guest())
        credit_note = self.order.invoices.get(kind=InvoiceKind.CREDIT_NOTE)

        response = self.client.get(self.url(credit_note))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Content-Disposition"],
            f'attachment; filename="creditnota_{credit_note.number}.pdf"',
        )
        self.assertTrue(response.content.startswith(b"%PDF"))
        # The invoice itself stays available.
        self.assertEqual(self.client.get(self.url()).status_code, 200)


class InvoiceOfAnotherOrderTests(InvoiceDownloadTestCase):
    """Security (IDOR): a token only opens the invoices of its own order."""

    def setUp(self):
        super().setUp()
        with commit_hooks():
            self.other_order = book(self.room, in_days(20), in_days(21))
        self.other_invoice = self.other_order.invoices.get()

    def test_invoice_of_another_order_is_404(self):
        response = self.client.get(self.url(self.other_invoice))
        self.assertEqual(response.status_code, 404)
        self.assertNotEqual(response.get("Content-Type"), "application/pdf")
        # The other way around as well.
        response = self.client.get(self.url(self.invoice, order=self.other_order))
        self.assertEqual(response.status_code, 404)

    def test_same_answer_for_an_invoice_that_does_not_exist(self):
        response = self.client.get(
            reverse("bookings:invoice", args=[self.order.token, 999999])
        )
        self.assertEqual(response.status_code, 404)

    def test_unknown_token_is_404(self):
        response = self.client.get(
            reverse("bookings:invoice", args=[uuid.uuid4(), self.invoice.pk])
        )
        self.assertEqual(response.status_code, 404)

    def test_order_without_invoices(self):
        legacy = OrderFactory(hotel=self.room.hotel, room=self.room)
        response = self.client.get(self.url(order=legacy))
        self.assertEqual(response.status_code, 404)

    def test_only_numbers_and_tokens_match_the_url(self):
        for path in [
            f"/boeking/{self.order.pk}/factuur/{self.invoice.pk}/",
            f"/boeking/{self.order.token}/factuur/{self.invoice.number}/",
        ]:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)


class InvoiceDownloadLimitTests(InvoiceDownloadTestCase):
    """Like the confirmation PDF: per IP address per minute."""

    @override_settings(BOOKING_PDF_LIMIT_PER_IP_PER_MINUTE=3)
    def test_downloads_per_ip_address_per_minute(self):
        with freeze_time("2027-03-01T09:00:00Z") as frozen:
            statuses = [self.client.get(self.url()).status_code for _ in range(4)]
            self.assertEqual(statuses, [200, 200, 200, 429])

            response = self.client.get(self.url())
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response["Retry-After"], "60")
            self.assert_private(response)

            other_ip = Client(REMOTE_ADDR="192.0.2.50")
            self.assertEqual(other_ip.get(self.url()).status_code, 200)

            frozen.tick(timedelta(seconds=61))
            self.assertEqual(self.client.get(self.url()).status_code, 200)

    @override_settings(BOOKING_PDF_LIMIT_PER_IP_PER_MINUTE=2)
    def test_guessing_counts_too(self):
        for _ in range(2):
            self.client.get(
                reverse("bookings:invoice", args=[uuid.uuid4(), self.invoice.pk])
            )
        self.assertEqual(self.client.get(self.url()).status_code, 429)

    @override_settings(BOOKING_PDF_LIMIT_PER_IP_PER_MINUTE=1)
    def test_counted_apart_from_the_confirmation_pdf(self):
        pdf_url = reverse("bookings:pdf", args=[self.order.token])
        self.assertEqual(self.client.get(pdf_url).status_code, 200)
        self.assertEqual(self.client.get(self.url()).status_code, 200)
        self.assertEqual(self.client.get(self.url()).status_code, 429)
