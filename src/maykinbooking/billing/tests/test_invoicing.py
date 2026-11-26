"""Invoices and credit notes: numbering, amounts, snapshot, immutability, PDF."""
import hashlib
import json
import os
import stat
import tempfile
from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import Permission
from django.core import serializers
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from freezegun import freeze_time

from maykinbooking.accounts.tests.factories import StaffUserFactory, SuperUserFactory
from maykinbooking.base.models import Order, OrderStatus, PaymentStatus
from maykinbooking.base.tests.factories import HotelFactory, OrderFactory, RoomFactory
from maykinbooking.bookings.services.booking import Actor, cancel_booking

from ..checks import check_private_media_root
from ..models import (
    DemoDataPurgeRefused,
    ImmutableInvoiceError,
    Invoice,
    InvoiceKind,
    InvoiceLine,
    InvoiceLineKind,
    InvoiceSequence,
    PricingSettings,
    check_demo_data_purge,
    purge_for_demo_data,
)
from ..services.invoicing import (
    _spread_revenue,
    invoice_pdf_bytes,
    issue_credit_note,
    issue_invoice,
    seller_details,
    store_invoice_pdf,
)
from ..storage import get_invoice_storage
from .factories import DiscountCodeFactory
from .utils import TempMediaMixin, book, temp_private_media

SELLER = {
    "INVOICE_SELLER_NAME": "Maykin Booking B.V.",
    "INVOICE_SELLER_ADDRESS": "Kingsfordweg 151\\n1043 GR Amsterdam",
    "INVOICE_SELLER_KVK": "12345678",
    "INVOICE_SELLER_VAT_ID": "NL001234567B01",
}
# Thursday 26 November 2026; the stays are in March 2027 (Monday 1 March).
NOW = "2026-11-26T10:00:00Z"


@freeze_time(NOW)
@override_settings(**SELLER)
class InvoiceTestCase(TempMediaMixin, TestCase):
    def setUp(self):
        self.hotel = HotelFactory(
            is_available=True, name="Hotel de Gracht", address="Keizersgracht 1"
        )
        self.room = RoomFactory(
            hotel=self.hotel,
            room_type="Double",
            price=Decimal("100.00"),
            is_available=True,
        )

    def book(self, start=date(2027, 3, 4), end=date(2027, 3, 7), **kwargs):
        return book(self.room, start, end, **kwargs)


class IssueInvoiceTests(InvoiceTestCase):
    def test_booking_at_the_hotel_gets_an_invoice(self):
        settings_row = PricingSettings.load()
        settings_row.weekend_percent = Decimal("20")
        settings_row.save()

        order = self.book()  # Thursday to Sunday: V1, 340.00

        invoice = Invoice.objects.get(order=order)
        self.assertEqual(invoice.number, "2026-00001")
        self.assertEqual((invoice.year, invoice.sequence), (2026, 1))
        self.assertEqual(invoice.kind, InvoiceKind.INVOICE)
        self.assertEqual(invoice.issue_date, date(2026, 11, 26))
        self.assertEqual(invoice.total_incl, Decimal("340.00"))
        self.assertEqual(invoice.total_excl, Decimal("280.99"))
        self.assertEqual(invoice.vat_amount, Decimal("59.01"))
        self.assertEqual(invoice.vat_rate, Decimal("21.00"))
        self.assertEqual(invoice.vat_country, "NL")
        self.assertFalse(invoice.is_test)
        self.assertEqual(invoice.payment_note, "Te betalen in het hotel.")
        # Snapshot of seller, customer and stay.
        self.assertEqual(invoice.seller_name, "Maykin Booking B.V.")
        self.assertEqual(invoice.seller_address, "Kingsfordweg 151\n1043 GR Amsterdam")
        self.assertEqual(invoice.seller_kvk, "12345678")
        self.assertEqual(invoice.seller_vat_id, "NL001234567B01")
        self.assertEqual(invoice.customer_name, "Alex de Landgraaf")
        self.assertEqual(invoice.customer_email, "alex@example.com")
        self.assertEqual(
            invoice.customer_address, "Keizersgracht 117\n1015CJ Amsterdam\nNederland"
        )
        self.assertEqual(invoice.hotel_name, "Hotel de Gracht")
        self.assertTrue(invoice.hotel_address.startswith("Keizersgracht 1, "))
        self.assertEqual(invoice.room_type, "Double")
        self.assertEqual(
            (invoice.start_date, invoice.end_date, invoice.nights),
            (date(2027, 3, 4), date(2027, 3, 7), 3),
        )

        lines = list(invoice.lines.all())
        self.assertEqual([line.position for line in lines], [1, 2, 3])
        self.assertEqual({line.kind for line in lines}, {InvoiceLineKind.NIGHT})
        self.assertEqual(
            [line.amount_incl for line in lines],
            [Decimal("100.00"), Decimal("120.00"), Decimal("120.00")],
        )
        self.assertEqual(
            [line.date for line in lines],
            [date(2027, 3, 4), date(2027, 3, 5), date(2027, 3, 6)],
        )
        self.assertEqual(
            lines[1].description, "Overnachting vrijdag 5 maart 2027 (weekend +20%)"
        )
        self.assertEqual(sum(line.revenue_excl for line in lines), invoice.total_excl)

    def test_discounts_are_lines_and_the_revenue_is_spread_over_the_nights(self):
        settings_row = PricingSettings.load()
        settings_row.long_stay_percent = Decimal("10")
        settings_row.save()

        order = self.book(date(2027, 3, 1), date(2027, 3, 8))  # V2: 630.00

        invoice = order.invoices.get()
        lines = list(invoice.lines.all())
        self.assertEqual(len(lines), 8)
        discount = lines[-1]
        self.assertEqual(discount.kind, InvoiceLineKind.DISCOUNT)
        self.assertEqual(discount.description, "Korting lang verblijf (10%)")
        self.assertEqual(discount.amount_incl, Decimal("-70.00"))
        self.assertEqual(discount.revenue_excl, Decimal("0.00"))
        self.assertEqual(invoice.total_incl, Decimal("630.00"))
        self.assertEqual(invoice.total_excl, Decimal("520.66"))
        self.assertEqual(invoice.vat_amount, Decimal("109.34"))
        self.assertEqual(sum(line.amount_incl for line in lines), invoice.total_incl)
        self.assertEqual(sum(line.revenue_excl for line in lines), invoice.total_excl)
        # The revenue per night: 520.66 / 7, the last night gets the rest.
        self.assertEqual(
            [line.revenue_excl for line in lines[:7]],
            [Decimal("74.38")] * 6 + [Decimal("74.38")],
        )

    def test_rate_of_the_check_in_date(self):
        with freeze_time("2025-12-20T10:00:00Z"):
            order = self.book(date(2025, 12, 31), date(2026, 1, 2))
        invoice = order.invoices.get()
        self.assertEqual(invoice.number, "2025-00001")
        self.assertEqual(invoice.vat_rate, Decimal("9.00"))
        self.assertEqual(invoice.total_incl, Decimal("200.00"))
        self.assertEqual(invoice.total_excl, Decimal("183.49"))
        self.assertEqual(invoice.vat_amount, Decimal("16.51"))

    def test_issue_invoice_is_idempotent(self):
        order = self.book()
        invoice = order.invoices.get()
        with transaction.atomic():
            locked = Order.objects.select_for_update().get(pk=order.pk)
            self.assertEqual(issue_invoice(locked), invoice)
        self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(InvoiceSequence.objects.get(year=2026).last_number, 1)

    def test_only_confirmed_orders(self):
        order = OrderFactory(status=OrderStatus.PENDING_PAYMENT)
        with self.assertRaises(ValueError):
            issue_invoice(order)
        self.assertFalse(Invoice.objects.exists())


class WatermarkTests(InvoiceTestCase):
    def test_complete_seller_and_rate_is_a_real_invoice(self):
        self.assertTrue(seller_details().complete)
        self.assertFalse(self.book().invoices.get().is_test)

    def test_missing_seller_detail_is_a_test_invoice(self):
        for setting in SELLER:
            with self.subTest(setting=setting), override_settings(**{setting: " "}):
                order = self.book()
                self.assertTrue(order.invoices.get().is_test)
                Order.objects.filter(pk=order.pk).update(status=OrderStatus.CANCELLED)

    def test_country_without_a_rate_is_a_test_invoice(self):
        self.hotel.country = "BE"
        self.hotel.save()
        invoice = self.book().invoices.get()
        self.assertTrue(invoice.is_test)
        self.assertIsNone(invoice.vat_rate)
        self.assertEqual(invoice.vat_country, "BE")
        self.assertEqual(invoice.total_excl, invoice.total_incl)
        self.assertEqual(invoice.vat_amount, Decimal("0.00"))

    def test_the_pdf_shows_the_watermark_only_on_test_invoices(self):
        self.hotel.country = "BE"
        self.hotel.save()
        test_invoice = self.book().invoices.get()
        self.hotel.country = "NL"
        self.hotel.save()
        real_invoice = self.book(date(2027, 4, 1), date(2027, 4, 2)).invoices.get()
        for invoice, watermark in [(test_invoice, True), (real_invoice, False)]:
            with self.subTest(number=invoice.number):
                html = render_to_string(
                    "billing/invoice_pdf.html",
                    {"invoice": invoice, "lines": list(invoice.lines.all())},
                )
                self.assertEqual("Testfactuur – niet geldig" in html, watermark)
                self.assertEqual("TESTFACTUUR – NIET GELDIG" in html, watermark)
                self.assertIn("Toeristenbelasting is niet inbegrepen", html)


class CreditNoteTests(InvoiceTestCase):
    def test_cancelling_credits_the_whole_invoice(self):
        order = self.book()
        invoice = order.invoices.get()

        cancel_booking(order.pk, actor=Actor.guest())

        credit_note = order.invoices.get(kind=InvoiceKind.CREDIT_NOTE)
        self.assertEqual(credit_note.number, "2026-00002")
        self.assertEqual(credit_note.credited_invoice, invoice)
        self.assertEqual(credit_note.filename, "creditnota_2026-00002.pdf")
        self.assertEqual(credit_note.total_incl, -invoice.total_incl)
        self.assertEqual(credit_note.total_excl, -invoice.total_excl)
        self.assertEqual(credit_note.vat_amount, -invoice.vat_amount)
        for field in (
            "seller_name",
            "customer_name",
            "customer_address",
            "hotel_name",
            "vat_rate",
            "is_test",
            "start_date",
            "nights",
        ):
            self.assertEqual(getattr(credit_note, field), getattr(invoice, field))
        self.assertEqual(
            [
                (line.description, line.amount_incl, line.revenue_excl)
                for line in credit_note.lines.all()
            ],
            [
                (line.description, -line.amount_incl, -line.revenue_excl)
                for line in invoice.lines.all()
            ],
        )
        self.assertEqual(credit_note.payment_note, "Er hoeft niets betaald te worden.")

    def test_one_credit_note_per_invoice(self):
        order = self.book()
        invoice = order.invoices.get()
        cancel_booking(order.pk, actor=Actor.guest())
        credit_note = order.invoices.get(kind=InvoiceKind.CREDIT_NOTE)
        self.assertEqual(issue_credit_note(invoice), credit_note)
        with self.assertRaises(ValueError):
            issue_credit_note(credit_note)
        self.assertEqual(Invoice.objects.count(), 2)

    def test_legacy_order_without_invoice_gets_no_credit_note(self):
        order = OrderFactory(
            hotel=self.hotel,
            room=self.room,
            start_date=date(2027, 3, 4),
            end_date=date(2027, 3, 6),
        )
        cancel_booking(order.pk, actor=Actor.guest())
        self.assertFalse(Invoice.objects.exists())


class NumberingTests(InvoiceTestCase):
    def test_a_rolled_back_invoice_leaves_no_gap(self):
        with self.assertRaises(RuntimeError), transaction.atomic():
            self.book()
            self.assertEqual(Invoice.objects.get().number, "2026-00001")
            raise RuntimeError("rollback")
        self.assertFalse(Invoice.objects.exists())
        self.assertEqual(self.book().invoices.get().number, "2026-00001")

    def test_one_series_per_year(self):
        with freeze_time("2026-12-31T22:00:00Z"):  # 23:00 in Amsterdam
            first = self.book().invoices.get()
        with freeze_time("2026-12-31T23:30:00Z"):  # 00:30 on 1 January
            second = self.book(date(2027, 4, 1), date(2027, 4, 2)).invoices.get()
        self.assertEqual(first.number, "2026-00001")
        self.assertEqual(second.number, "2027-00001")
        self.assertEqual(second.issue_date, date(2027, 1, 1))


class ImmutabilityTests(InvoiceTestCase):
    def setUp(self):
        super().setUp()
        self.invoice = self.book().invoices.get()
        self.line = self.invoice.lines.first()

    def test_saving_and_deleting_raises(self):
        self.invoice.total_incl = Decimal("1.00")
        for action in [
            self.invoice.save,
            self.invoice.delete,
            self.line.save,
            self.line.delete,
            lambda: Invoice.objects.filter(pk=self.invoice.pk).update(total_incl=1),
            lambda: Invoice.objects.update(total_incl=1),
            lambda: Invoice.objects.all().delete(),
            lambda: InvoiceLine.objects.all().update(amount_incl=1),
            lambda: InvoiceLine.objects.filter(pk=self.line.pk).delete(),
            lambda: self.invoice.lines.all().delete(),
            lambda: Invoice.objects.bulk_update([self.invoice], ["total_incl"]),
        ]:
            with self.subTest(action=action):
                with self.assertRaises(ImmutableInvoiceError):
                    action()
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.total_incl, Decimal("300.00"))
        self.assertEqual(self.invoice.lines.count(), 3)

    def test_a_new_instance_with_an_existing_pk_does_not_overwrite(self):
        """Security V6 (1): saving a new object with the pk of an issued one."""
        forged = Invoice.objects.get(pk=self.invoice.pk)
        forged._state.adding = True
        forged.total_incl = Decimal("1.00")
        with self.assertRaises(IntegrityError), transaction.atomic():
            forged.save()
        line = InvoiceLine.objects.get(pk=self.line.pk)
        line._state.adding = True
        line.amount_incl = Decimal("1.00")
        with self.assertRaises(IntegrityError), transaction.atomic():
            line.save()
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.total_incl, Decimal("300.00"))
        self.assertEqual(
            InvoiceLine.objects.get(pk=self.line.pk).amount_incl, Decimal("100.00")
        )
        with self.assertRaises(ImmutableInvoiceError):
            forged.save(update_fields=["total_incl"])

    def test_save_base_only_inserts(self):
        """Security V6: a direct call of save_base does not update either."""
        invoice = Invoice.objects.get(pk=self.invoice.pk)
        invoice.total_incl = Decimal("1.00")
        with self.assertRaises(ImmutableInvoiceError):
            invoice.save_base()
        invoice._state.adding = True
        with self.assertRaises(IntegrityError), transaction.atomic():
            invoice.save_base()
        with self.assertRaises(ImmutableInvoiceError):
            invoice.save_base(force_update=True)
        with self.assertRaises(ImmutableInvoiceError):
            Invoice.objects.filter(pk=invoice.pk)._update({"total_incl": 1})
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.total_incl, Decimal("300.00"))

    def test_a_fixture_cannot_overwrite_an_issued_invoice(self):
        """Security V6: ``loaddata`` with the pk of an issued invoice."""
        for model, pk, field in (
            (Invoice, self.invoice.pk, "total_incl"),
            (InvoiceLine, self.line.pk, "amount_incl"),
        ):
            with self.subTest(model=model.__name__):
                data = json.loads(
                    serializers.serialize("json", model.objects.filter(pk=pk))
                )
                data[0]["fields"][field] = "1.00"
                with tempfile.TemporaryDirectory() as directory:
                    fixture = os.path.join(directory, "forged.json")
                    with open(fixture, "w") as file:
                        json.dump(data, file)
                    with self.assertRaises(ImmutableInvoiceError):
                        call_command("loaddata", fixture, verbosity=0)
                self.assertNotEqual(
                    getattr(model.objects.get(pk=pk), field), Decimal("1.00")
                )

    def test_the_base_manager_is_immutable_too(self):
        """Security V6 (2): Django's own updates go through the base manager."""
        for model in (Invoice, InvoiceLine):
            with self.subTest(model=model.__name__):
                with self.assertRaises(ImmutableInvoiceError):
                    model._base_manager.all().update(pk=1)
                with self.assertRaises(ImmutableInvoiceError):
                    model._base_manager.all().delete()

    def test_the_user_who_issued_an_invoice_cannot_be_deleted(self):
        """
        Security V6 (3): SET_NULL would change the invoice when the user is
        deleted; PROTECT keeps it as issued (users are deactivated instead).
        """
        staff = StaffUserFactory()
        cancel_booking(
            self.invoice.order_id,
            actor=Actor.staff(staff),
            reason="Op verzoek",
            enforce_deadline=False,
        )
        credit_note = Invoice.objects.get(kind=InvoiceKind.CREDIT_NOTE)
        self.assertEqual(credit_note.created_by, staff)
        with self.assertRaises(ProtectedError):
            staff.delete()
        credit_note.refresh_from_db()
        self.assertEqual(credit_note.created_by, staff)

    def test_the_order_cannot_be_deleted(self):
        from django.db.models import ProtectedError

        with self.assertRaises(ProtectedError):
            Order.objects.filter(pk=self.invoice.order_id).delete()

    def test_the_pdf_can_be_attached_once(self):
        self.assertTrue(Invoice.objects._attach_pdf(self.invoice.pk, "a.pdf", "1" * 64))
        self.assertFalse(
            Invoice.objects._attach_pdf(self.invoice.pk, "b.pdf", "2" * 64)
        )
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.pdf.name, "a.pdf")

    def test_purge_for_the_demo_data_never_deletes_real_invoices(self):
        # The seller details are complete: these are real invoices.
        cancel_booking(self.invoice.order_id, actor=Actor.guest())
        with self.assertRaisesMessage(DemoDataPurgeRefused, "2 real invoices"):
            purge_for_demo_data()
        self.assertEqual(Invoice.objects.count(), 2)


@freeze_time(NOW)
class DemoDataPurgeTests(TempMediaMixin, TestCase):
    """Security V3: only where demo data belongs, and only test invoices."""

    def setUp(self):
        room = RoomFactory(
            hotel=HotelFactory(is_available=True), price=Decimal("100.00")
        )
        # No seller details: test invoices (with the watermark).
        self.order = book(room, date(2027, 3, 4), date(2027, 3, 6))
        cancel_booking(self.order.pk, actor=Actor.guest())

    def test_test_invoices_are_deleted_and_the_numbers_kept(self):
        self.assertFalse(Invoice.objects.filter(is_test=False).exists())
        counts = purge_for_demo_data()
        self.assertEqual(counts["facturen"], 2)
        self.assertFalse(Invoice.objects.exists())
        self.assertFalse(InvoiceLine.objects.exists())
        # The numbers are never used again.
        self.assertEqual(InvoiceSequence.objects.get(year=2026).last_number, 2)

    def test_only_with_debug_or_in_a_development_environment(self):
        for environment, debug, allowed in [
            ("development", False, True),
            ("ci", False, True),
            ("jenkins", False, True),
            ("staging", True, True),
            ("staging", False, False),
            ("production", False, False),
            ("", False, False),
        ]:
            with self.subTest(environment=environment, debug=debug):
                with override_settings(ENVIRONMENT=environment, DEBUG=debug):
                    if allowed:
                        check_demo_data_purge()
                    else:
                        with self.assertRaises(DemoDataPurgeRefused):
                            purge_for_demo_data()
        self.assertEqual(Invoice.objects.count(), 2)


class InvoicePdfTests(InvoiceTestCase):
    def test_pdf_is_stored_once_after_the_commit(self):
        # A directory of its own: the files of other tests do not count.
        with temp_private_media() as private_media:
            with self.captureOnCommitCallbacks(execute=True):
                order = self.book()
            invoice = order.invoices.get()
            self.assertRegex(invoice.pdf.name, r"^2026/2026-00001-[0-9a-f]{16}\.pdf$")
            path = os.path.join(private_media, "invoices", invoice.pdf.name)
            with open(path, "rb") as file:
                content = file.read()
            self.assertTrue(content.startswith(b"%PDF"))
            self.assertEqual(invoice.pdf_sha256, hashlib.sha256(content).hexdigest())
            self.assertEqual(invoice_pdf_bytes(invoice), content)

            # A second run keeps the first file.
            store_invoice_pdf(invoice.pk)
            invoice.refresh_from_db()
            self.assertEqual(
                os.listdir(os.path.dirname(path)), [os.path.basename(invoice.pdf.name)]
            )

    def test_only_the_site_can_read_the_pdfs(self):
        with temp_private_media() as private_media:
            with self.captureOnCommitCallbacks(execute=True):
                order = self.book()
            invoice = order.invoices.get()
            path = os.path.join(private_media, "invoices", invoice.pdf.name)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            for directory in (
                os.path.dirname(path),
                os.path.join(private_media, "invoices"),
            ):
                self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o700)

    def test_pdfs_are_outside_media_root_and_have_no_url(self):
        with self.captureOnCommitCallbacks(execute=True):
            order = self.book()
        invoice = order.invoices.get()
        location = get_invoice_storage().path(invoice.pdf.name)
        self.assertTrue(
            location.startswith(os.path.join(self.private_media_root, "invoices"))
        )
        self.assertFalse(
            os.path.abspath(location).startswith(os.path.abspath(self.media_root))
        )
        self.assertEqual(os.listdir(self.media_root), [])
        with self.assertRaises(ValueError):
            get_invoice_storage().url(invoice.pdf.name)
        response = self.client.get(f"/media/invoices/{invoice.pdf.name}")
        self.assertEqual(response.status_code, 404)

    def test_missing_pdf_is_rendered_again(self):
        with self.captureOnCommitCallbacks(execute=True):
            order = self.book()
        invoice = order.invoices.get()
        get_invoice_storage().delete(invoice.pdf.name)
        with self.assertLogs("maykinbooking.billing", "ERROR"):
            content = invoice_pdf_bytes(invoice)
        self.assertTrue(content.startswith(b"%PDF"))

    def test_pdf_that_was_never_stored_is_stored_on_request(self):
        order = self.book()  # no commit callbacks: nothing stored
        invoice = order.invoices.get()
        self.assertFalse(invoice.pdf)
        content = invoice_pdf_bytes(invoice)
        invoice.refresh_from_db()
        self.assertTrue(invoice.pdf)
        self.assertEqual(invoice.pdf_sha256, hashlib.sha256(content).hexdigest())

    def test_storing_never_raises(self):
        order = self.book()
        invoice = order.invoices.get()
        with mock.patch(
            "maykinbooking.billing.services.invoicing.render_invoice_pdf",
            side_effect=RuntimeError("weasyprint weg"),
        ), self.assertLogs("maykinbooking.billing", "ERROR"):
            store_invoice_pdf(invoice.pk)
        invoice.refresh_from_db()
        self.assertFalse(invoice.pdf)
        store_invoice_pdf(10**6)  # unknown: logged, not raised


@freeze_time(NOW)
class InvoiceAdminTests(TempMediaMixin, TestCase):
    def setUp(self):
        room = RoomFactory(hotel=HotelFactory(is_available=True), is_available=True)
        self.invoice = book(room, date(2027, 3, 4), date(2027, 3, 5)).invoices.get()

    def test_read_only_for_the_superuser(self):
        self.client.force_login(SuperUserFactory())
        url = reverse("admin:billing_invoice_change", args=[self.invoice.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="_save"')
        self.assertEqual(self.client.post(url, {"number": "X"}).status_code, 403)
        delete_url = reverse("admin:billing_invoice_delete", args=[self.invoice.pk])
        self.assertEqual(self.client.get(delete_url).status_code, 403)
        add_url = reverse("admin:billing_invoice_add")
        self.assertEqual(self.client.get(add_url).status_code, 403)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.number, "2026-00001")

    def test_invoice_with_a_stored_pdf(self):
        """
        Review B1: the PDF field itself would make the admin fail (its storage
        has no URL). The page shows the file name and a link to the dashboard.
        """
        store_invoice_pdf(self.invoice.pk)
        self.invoice.refresh_from_db()
        self.assertTrue(self.invoice.pdf)
        self.client.force_login(SuperUserFactory())
        response = self.client.get(
            reverse("admin:billing_invoice_change", args=[self.invoice.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            f'<a href="{reverse("dashboard:invoice_pdf", args=[self.invoice.pk])}">'
            f"{self.invoice.filename}</a>",
            html=True,
        )
        # Never the stored name (with its random part) or a media URL.
        self.assertNotContains(response, self.invoice.pdf.name)
        self.assertNotContains(response, "/media/")
        response = self.client.get(reverse("admin:billing_invoice_changelist"))
        self.assertEqual(response.status_code, 200)

    def test_invoice_without_a_pdf_yet(self):
        self.client.force_login(SuperUserFactory())
        response = self.client.get(
            reverse("admin:billing_invoice_change", args=[self.invoice.pk])
        )
        self.assertContains(response, "(nog niet opgeslagen)")

    def test_vat_rates_only_for_the_superuser(self):
        staff = StaffUserFactory()
        staff.user_permissions.set(
            Permission.objects.filter(content_type__app_label="billing")
        )
        self.client.force_login(staff)
        url = reverse("admin:billing_vatrate_changelist")
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(SuperUserFactory())
        self.assertEqual(self.client.get(url).status_code, 200)


class PaidOrderInvoiceTests(InvoiceTestCase):
    def test_paid_online_note(self):
        order = OrderFactory(
            hotel=self.hotel,
            room=self.room,
            start_date=date(2027, 3, 4),
            end_date=date(2027, 3, 6),
            payment_status=PaymentStatus.PAID,
        )
        with transaction.atomic():
            invoice = issue_invoice(Order.objects.select_for_update().get(pk=order.pk))
        self.assertTrue(
            invoice.payment_note.startswith("Betaald via iDEAL op 26 november 2026")
        )


class SpreadRevenueTests(SimpleTestCase):
    """QA B8: the revenue per night is never negative and adds up exactly."""

    def spread(self, prices, total_excl):
        lines = [InvoiceLine(amount_incl=Decimal(price)) for price in prices]
        _spread_revenue(lines, Decimal(total_excl))
        return [line.revenue_excl for line in lines]

    def test_rounding_up_every_night_no_longer_leaves_a_negative_last_night(self):
        # 0.03 over 10/10/0: 0.015 each. Rounding each half up gave 0.02 + 0.02
        # and -0.01 for the last night.
        shares = self.spread(["10.00", "10.00", "0.00"], "0.03")
        self.assertEqual(shares, [Decimal("0.02"), Decimal("0.01"), Decimal("0.00")])

    def test_shares_add_up_and_follow_the_prices(self):
        for prices, total in [
            (["150", "150", "180", "180", "150", "150", "150", "100"], "900.00"),
            (["0.01"] * 7, "0.05"),
            (["99.99", "0.01", "45.50"], "120.37"),
            (["100"], "82.64"),
            (["0", "0"], "0.00"),
        ]:
            with self.subTest(prices=prices, total=total):
                shares = self.spread(prices, total)
                self.assertEqual(sum(shares), Decimal(total))
                self.assertTrue(all(share >= 0 for share in shares), shares)
                exact = [
                    Decimal(total) * Decimal(price) / sum(map(Decimal, prices))
                    if sum(map(Decimal, prices))
                    else Decimal(0)
                    for price in prices
                ]
                for share, value in zip(shares, exact):
                    self.assertLess(abs(share - value), Decimal("0.01"))


@freeze_time(NOW)
class InvoiceDetailTests(InvoiceTestCase):
    def test_a_free_stay_is_not_to_be_paid_at_the_hotel(self):
        DiscountCodeFactory(code="GRATIS", kind="fixed", value=Decimal("1000"))
        invoice = self.book(discount_code="GRATIS").invoices.get()
        self.assertEqual(invoice.total_incl, Decimal("0.00"))
        self.assertEqual(invoice.payment_note, "Er hoeft niets betaald te worden.")

    def test_a_long_hotel_address_is_cut_off(self):
        self.hotel.address = "Straat " + "x" * 193
        self.hotel.city.name = "y" * 100
        self.hotel.city.save()
        self.hotel.save()
        invoice = self.book().invoices.get()
        self.assertEqual(len(invoice.hotel_address), 300)


class PrivateMediaRootCheckTests(SimpleTestCase):
    """Security advice: the invoices never in the media or static files."""

    def test_inside_media_or_static_is_an_error(self):
        for private, media, static in [
            ("/srv/site/media/invoices", "/srv/site/media", "/srv/site/static"),
            ("/srv/site/media", "/srv/site/media", "/srv/site/static"),
            ("/srv/site/static/private", "/srv/site/media", "/srv/site/static"),
        ]:
            with self.subTest(private=private):
                with override_settings(
                    PRIVATE_MEDIA_ROOT=private, MEDIA_ROOT=media, STATIC_ROOT=static
                ):
                    errors = check_private_media_root(None)
                self.assertEqual([error.id for error in errors], ["billing.E001"])

    def test_beside_them_is_fine(self):
        with override_settings(
            PRIVATE_MEDIA_ROOT="/srv/site/private_media",
            MEDIA_ROOT="/srv/site/media",
            STATIC_ROOT="/srv/site/static",
        ):
            self.assertEqual(check_private_media_root(None), [])
        # The settings of this run.
        self.assertEqual(check_private_media_root(None), [])
