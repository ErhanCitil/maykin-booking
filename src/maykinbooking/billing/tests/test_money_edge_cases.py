"""
QA, phase 2 "Geld": edge cases.

* A high season over the turn of the year (price, invoice, revenue per month).
* VAT 9% versus 21% around 1 January 2026 (the check-in date decides), and a
  credit note in the new year for an invoice of the old one.
* A fixed discount code above the total.
* A code whose ``max_uses`` is reached; a cancellation keeps counting, an
  expired order gives its use back.
* The reception on every dashboard URL (GET and POST) and in the admin.
* The webhook with an unknown or malformed id, another amount or currency.
* The fake provider switched off.

The expectations come from the requirements and the design (section 6), worked
out by hand; ``freeze_time`` is set per class.
"""
import csv
import io
from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib import admin
from django.contrib.messages import get_messages
from django.core import mail
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse

import requests_mock
from freezegun import freeze_time

from maykinbooking.accounts.tests.factories import (
    ManagerUserFactory,
    ReceptionUserFactory,
)
from maykinbooking.base.models import (
    ActorType,
    City,
    EmailKind,
    Hotel,
    Order,
    OrderStatus,
    PaymentStatus,
    Room,
)
from maykinbooking.base.tests.factories import RoomBlockFactory
from maykinbooking.bookings.services.booking import (
    Actor,
    OnlinePaymentUnavailable,
    PaymentMethod,
    cancel_booking,
    quote,
)

from ..models import (
    DiscountKind,
    DiscountRedemption,
    FakeProviderPayment,
    Invoice,
    InvoiceKind,
    Payment,
    PaymentState,
    PricingSettings,
    ProviderName,
    Refund,
    RefundReason,
    RefundState,
    SeasonPeriod,
)
from ..providers import online_payment_available
from ..services.invoicing import vat_rate_for
from ..services.payments import retry_refund, start_online_payment, sync_payment
from .factories import DiscountCodeFactory, PaymentFactory, SeasonPeriodFactory
from .money_flows import SITE_URL, CheckoutDriver, guest
from .utils import (
    MOLLIE_API,
    MOLLIE_TEST_KEY,
    TempMediaMixin,
    book,
    commit_hooks,
    mollie_payment,
    mollie_refund,
    mollie_refund_list,
    pay_at_provider,
)

D = Decimal
INVALID_CODE = "Deze kortingscode is niet geldig."
ONLINE_REFUSED = "Online betalen is voor deze boeking niet mogelijk."


def night_revenue(invoice):
    return list(
        invoice.lines.filter(kind="night")
        .order_by("position")
        .values_list("revenue_excl", flat=True)
    )


@override_settings(
    SITE_URL=SITE_URL,
    MOLLIE_API_KEY="",
    PAYMENT_FAKE_PROVIDER=True,
    PAYMENT_HOLD_MINUTES=15,
    BOOKING_LIMIT_PER_IP_PER_HOUR=100,
    BOOKING_LIMIT_PER_EMAIL_PER_DAY=100,
)
class EdgeCaseTestCase(CheckoutDriver, TempMediaMixin, TestCase):
    """A hotel with one room type; subclasses set ``freeze_time`` themselves."""

    price = D("100.00")
    inventory = 3

    @classmethod
    def setUpTestData(cls):
        cls.city = City.objects.create(city_id="AMS", name="Amsterdam")
        cls.hotel = Hotel.objects.create(
            city=cls.city, hotel_id="AMS01", name="Hotel de Gracht", is_available=True
        )
        cls.room = Room.objects.create(
            hotel=cls.hotel,
            room_type="Double",
            price=cls.price,
            description="Kamer",
            inventory=cls.inventory,
        )
        cls.manager = ManagerUserFactory()
        PricingSettings.load()

    def setUp(self):
        cache.clear()

    def staff_client(self, user):
        client = Client()
        client.force_login(user)
        return client

    def report_revenue(self, start, last):
        response = self.staff_client(self.manager).get(
            reverse("dashboard:report"),
            {"van": start.isoformat(), "tot": last.isoformat(), "hotel": self.hotel.pk},
        )
        (row,) = response.context["rows"]
        return row.revenue


# Season over the turn of the year


@freeze_time("2026-12-01T09:00:00Z")
class SeasonOverNewYearTests(EdgeCaseTestCase):
    """
    "Kerst" from 20 December 2026 up to and including 5 January 2027, +50%;
    weekend +20%; 10% off from 7 nights. 1 January 2027 is a Friday.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        PricingSettings.objects.filter(pk=1).update(
            weekend_percent=D("20"),
            long_stay_min_nights=7,
            long_stay_percent=D("10"),
        )
        SeasonPeriodFactory(
            name="Kerst",
            start_date=date(2026, 12, 20),
            end_date=date(2027, 1, 5),
            percent=D("50"),
        )

    def test_quote_over_the_turn_of_the_year(self):
        offer = quote(self.room, date(2026, 12, 30), date(2027, 1, 7))
        self.assertEqual(
            [(night.date, night.season_name, night.price) for night in offer.nights],
            [
                (date(2026, 12, 30), "Kerst", D("150.00")),  # Wednesday
                (date(2026, 12, 31), "Kerst", D("150.00")),
                (date(2027, 1, 1), "Kerst", D("180.00")),  # Friday: x1.2 x1.5
                (date(2027, 1, 2), "Kerst", D("180.00")),  # Saturday
                (date(2027, 1, 3), "Kerst", D("150.00")),
                (date(2027, 1, 4), "Kerst", D("150.00")),
                (date(2027, 1, 5), "Kerst", D("150.00")),  # last day of the season
                (date(2027, 1, 6), "", D("100.00")),
            ],
        )
        self.assertEqual(offer.subtotal, D("1210.00"))
        self.assertEqual(
            (offer.length_discount_percent, offer.length_discount),
            (D("10"), D("121.00")),
        )
        self.assertEqual(offer.total, D("1089.00"))

    def test_invoice_and_revenue_are_split_over_the_two_years(self):
        order = book(self.room, date(2026, 12, 30), date(2027, 1, 7))
        self.assertEqual(order.total_price, D("1089.00"))
        invoice = order.invoices.get()
        # Issued in 2026, check-in in 2026: 21%. 1089 / 1.21 = 900.00 exactly.
        self.assertEqual(invoice.number, "2026-00001")
        self.assertEqual(invoice.vat_rate, D("21.00"))
        self.assertEqual(
            (invoice.total_excl, invoice.vat_amount), (D("900.00"), D("189.00"))
        )
        # 900 pro rata to 150/150/180/180/150/150/150/100 of 1210, rounded
        # down (899.99 together); the cent that is left goes to the night that
        # lost most by rounding (1 January: 133.8843).
        self.assertEqual(
            night_revenue(invoice),
            [
                D("111.57"),
                D("111.57"),
                D("133.89"),
                D("133.88"),
                D("111.57"),
                D("111.57"),
                D("111.57"),
                D("74.38"),
            ],
        )
        december = self.report_revenue(date(2026, 12, 1), date(2026, 12, 31))
        january = self.report_revenue(date(2027, 1, 1), date(2027, 1, 31))
        self.assertEqual(december, D("223.14"))
        self.assertEqual(january, D("676.86"))
        self.assertEqual(december + january, invoice.total_excl)

    def test_dashboard_accepts_a_season_over_new_year_and_checks_overlap(self):
        manager = self.staff_client(self.manager)
        url = reverse("dashboard:season_create")
        response = manager.post(
            url,
            {
                "name": "Winter",
                "start_date": "2027-12-20",
                "end_date": "2028-01-05",
                "percent": "25",
            },
        )
        self.assertRedirects(
            response, reverse("dashboard:pricing_rules"), fetch_redirect_response=False
        )
        # 5 January is still "Winter".
        response = manager.post(
            url,
            {
                "name": "Nieuwjaar",
                "start_date": "2028-01-05",
                "end_date": "2028-01-10",
                "percent": "10",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "overlapt met het seizoen “Winter”")
        self.assertFalse(SeasonPeriod.objects.filter(name="Nieuwjaar").exists())
        response = manager.post(
            url,
            {
                "name": "Nieuwjaar",
                "start_date": "2028-01-06",
                "end_date": "2028-01-10",
                "percent": "10",
            },
        )
        self.assertEqual(response.status_code, 302)

        offer = quote(self.room, date(2028, 1, 4), date(2028, 1, 8))
        self.assertEqual(
            [night.season_name for night in offer.nights],
            ["Winter", "Winter", "Nieuwjaar", "Nieuwjaar"],
        )


# VAT around 1 January 2026


@freeze_time("2025-12-01T09:00:00Z")
class VatRateChangeTests(EdgeCaseTestCase):
    """Room of 170.00: two nights are 340.00 (design, the VAT examples)."""

    price = D("170.00")

    def test_check_in_on_31_december_2025_is_9_percent_for_the_whole_stay(self):
        order = book(self.room, date(2025, 12, 31), date(2026, 1, 2))
        invoice = order.invoices.get()
        self.assertEqual(invoice.number, "2025-00001")
        self.assertEqual(invoice.vat_rate, D("9.00"))
        self.assertEqual(
            (invoice.total_incl, invoice.total_excl, invoice.vat_amount),
            (D("340.00"), D("311.93"), D("28.07")),
        )
        # Also the night of 1 January 2026: the check-in date decides.
        self.assertEqual(
            list(invoice.lines.order_by("position").values_list("date", "amount_excl")),
            [(date(2025, 12, 31), D("155.96")), (date(2026, 1, 1), D("155.96"))],
        )
        self.assertEqual(sum(night_revenue(invoice)), D("311.93"))

    def test_check_in_on_1_january_2026_is_21_percent(self):
        order = book(self.room, date(2026, 1, 1), date(2026, 1, 3))
        invoice = order.invoices.get()
        self.assertEqual(invoice.vat_rate, D("21.00"))
        self.assertEqual(
            (invoice.total_excl, invoice.vat_amount), (D("280.99"), D("59.01"))
        )

    def test_credit_note_in_the_new_year_keeps_the_rate_of_the_invoice(self):
        order = book(self.room, date(2025, 12, 31), date(2026, 1, 2))
        staff = ManagerUserFactory()
        with freeze_time("2026-01-01T10:00:00Z"):
            # The guest can no longer cancel; staff can, until the stay is over.
            cancel_booking(
                order.pk,
                actor=Actor.staff(staff),
                reason="Gast ziek",
                enforce_deadline=False,
            )
            next_order = book(self.room, date(2026, 1, 10), date(2026, 1, 12))
        credit_note = order.invoices.get(kind=InvoiceKind.CREDIT_NOTE)
        self.assertEqual(credit_note.number, "2026-00001")
        self.assertEqual(credit_note.issue_date, date(2026, 1, 1))
        self.assertEqual(credit_note.vat_rate, D("9.00"))
        self.assertEqual(
            (
                credit_note.total_incl,
                credit_note.total_excl,
                credit_note.vat_amount,
            ),
            (D("-340.00"), D("-311.93"), D("-28.07")),
        )
        self.assertEqual(credit_note.created_by, staff)
        # The 2026 series goes on after the credit note.
        self.assertEqual(next_order.invoices.get().number, "2026-00002")
        self.assertEqual(next_order.invoices.get().vat_rate, D("21.00"))

    def test_rates_on_either_side_of_the_change(self):
        self.assertEqual(vat_rate_for("NL", date(2025, 12, 31)), D("9.00"))
        self.assertEqual(vat_rate_for("NL", date(2026, 1, 1)), D("21.00"))
        self.assertIsNone(vat_rate_for("BE", date(2026, 1, 1)))


# A fixed discount code above the total


@freeze_time("2027-03-01T09:00:00Z")
class FixedCodeAboveTotalTests(EdgeCaseTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.code = DiscountCodeFactory(
            code="GRATIS", kind=DiscountKind.FIXED, value=D("500.00")
        )

    def test_checkout_books_a_free_stay_at_the_hotel_only(self):
        start, end = date(2027, 3, 10), date(2027, 3, 12)
        url, response, total = self.to_confirm(self.room, start, end, code="gratis")
        self.assertEqual(total, "0.00")
        self.assertNotContains(response, "Nu online betalen (iDEAL)")

        # Online is refused for a stay that costs nothing; nothing is booked.
        response = self.confirm(url, total, payment_method="online")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, ONLINE_REFUSED)
        self.assertFalse(Order.objects.exists())

        response = self.confirm(url, total, payment_method="at_hotel")
        order = Order.objects.get()
        self.assertEqual(order.status, OrderStatus.CONFIRMED)
        # At most what there is: 200.00 off, not 500.00.
        self.assertEqual(
            (order.subtotal_price, order.code_discount, order.total_price),
            (D("200.00"), D("200.00"), D("0.00")),
        )
        self.assertEqual(order.discount_redemption.amount, D("200.00"))
        self.assertFalse(Payment.objects.exists())

        invoice = order.invoices.get()
        self.assertEqual(
            (invoice.total_incl, invoice.total_excl, invoice.vat_amount),
            (D("0.00"), D("0.00"), D("0.00")),
        )
        self.assertEqual(
            list(
                invoice.lines.order_by("position").values_list(
                    "kind", "amount_incl", "revenue_excl"
                )
            ),
            [
                ("night", D("100.00"), D("0.00")),
                ("night", D("100.00"), D("0.00")),
                ("discount", D("-200.00"), D("0.00")),
            ],
        )
        (confirmation,) = mail.outbox
        self.assertEqual(len(confirmation.attachments), 2)

        with commit_hooks():
            self.client.post(reverse("bookings:cancel", args=[order.token]))
        credit_note = order.invoices.get(kind=InvoiceKind.CREDIT_NOTE)
        self.assertEqual(credit_note.total_incl, D("0.00"))
        self.assertFalse(Refund.objects.exists())
        # No "-0,00" for the bookkeeping.
        export = self.staff_client(self.manager).get(
            reverse("dashboard:invoice_export")
        )
        content = b"".join(export.streaming_content).decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(content), delimiter=";"))
        self.assertEqual(len(rows), 3)
        for row in rows[1:]:
            self.assertEqual(row[14:17], ["0,00", "0,00", "0,00"], row)
        self.assertEqual(self.report_revenue(start, end), D("0.00"))

    def test_fixed_code_comes_after_the_long_stay_discount(self):
        PricingSettings.objects.filter(pk=1).update(
            long_stay_min_nights=7, long_stay_percent=D("10")
        )
        DiscountCodeFactory(code="DUIZEND", kind=DiscountKind.FIXED, value=D("1000"))
        offer = quote(
            self.room, date(2027, 3, 1), date(2027, 3, 8), discount_code="duizend"
        )
        # 700 - 70 = 630 left, so the code takes 630 and not 1000.
        self.assertEqual(
            (offer.subtotal, offer.length_discount, offer.code_discount, offer.total),
            (D("700.00"), D("70.00"), D("630.00"), D("0.00")),
        )

    def test_online_booking_of_a_free_stay_is_refused_and_uses_nothing(self):
        with self.assertRaises(OnlinePaymentUnavailable):
            book(
                self.room,
                date(2027, 3, 10),
                date(2027, 3, 12),
                payment_method=PaymentMethod.ONLINE,
                discount_code="GRATIS",
            )
        self.assertFalse(Order.objects.exists())
        self.code.refresh_from_db()
        self.assertEqual(self.code.times_used, 0)
        self.assertFalse(DiscountRedemption.objects.exists())


# max_uses


@freeze_time("2027-03-01T09:00:00Z")
class MaxUsesTests(EdgeCaseTestCase):
    def test_used_up_code_is_refused_until_an_expired_order_gives_its_use_back(self):
        code = DiscountCodeFactory(code="TWEE", value=D("10"), max_uses=2)
        start, end = date(2027, 3, 10), date(2027, 3, 12)
        first, second, third = Client(), Client(), Client()

        # 1. The first guest books at the hotel, the second starts paying online.
        self.book_at_hotel(self.room, start, end, code="TWEE", client=first)
        payment_page = self.start_online(
            self.room,
            start,
            end,
            code="TWEE",
            guest_data=guest("b@example.com"),
            client=second,
        )
        code.refresh_from_db()
        self.assertEqual(code.times_used, 2)

        # 2. The third guest is refused at step 1, with the general message.
        _url, response = self.stay_step(
            self.room, start, end, code="TWEE", client=third
        )
        self.assertEqual(response.context["wizard"]["steps"].current, "stay")
        self.assertContains(response, INVALID_CODE, count=1)
        response = self.staff_client(self.manager).get(
            reverse("dashboard:discount_list")
        )
        (row,) = response.context["rows"]
        self.assertEqual(str(row["status"][0]), "Op")

        # 3. A cancellation keeps counting.
        booked = Order.objects.get(status=OrderStatus.CONFIRMED)
        with commit_hooks():
            first.post(reverse("bookings:cancel", args=[booked.token]))
        code.refresh_from_db()
        self.assertEqual(code.times_used, 2)
        _url, response = self.stay_step(
            self.room, start, end, code="TWEE", client=third
        )
        self.assertContains(response, INVALID_CODE, count=1)

        # 4. The online payment fails: the order expires and gives its use back.
        self.pay_and_return(payment_page, action="failed", client=second)
        expired = Order.objects.get(status=OrderStatus.EXPIRED)
        code.refresh_from_db()
        self.assertEqual(code.times_used, 1)
        self.assertIsNotNone(expired.discount_redemption.released_at)

        # 5. Now the third guest gets it.
        response = self.book_at_hotel(
            self.room,
            start,
            end,
            code="TWEE",
            guest_data=guest("c@example.com"),
            client=third,
        )
        self.assertEqual(response.status_code, 302)
        code.refresh_from_db()
        self.assertEqual(code.times_used, 2)
        self.assertEqual(
            DiscountRedemption.objects.filter(code=code, released_at=None).count(), 2
        )
        self.assertEqual(Order.objects.filter(code_discount=D("20.00")).count(), 3)


# The reception on every URL


RECEPTION_GET = {
    # name: status of a GET for the reception (design, "Rollen en permissies")
    "home": 302,
    "booking_list": 200,
    "booking_export": 200,
    "booking_detail": 200,
    "booking_cancel": 200,
    "booking_resend": 405,  # POST only
    "booking_refund_retry": 405,  # POST only
}


@freeze_time("2027-03-01T09:00:00Z")
class ReceptionPermissionTests(EdgeCaseTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.reception = ReceptionUserFactory()
        cls.order = book(cls.room, date(2027, 3, 10), date(2027, 3, 12))
        cls.block = RoomBlockFactory(room=cls.room)
        cls.season = SeasonPeriodFactory()
        cls.code = DiscountCodeFactory()

    def url(self, name):
        args = {
            "hotel_edit": self.hotel.pk,
            "hotel_delete": self.hotel.pk,
            "room_create": self.hotel.pk,
            "room_edit": self.room.pk,
            "room_delete": self.room.pk,
            "room_blocks": self.room.pk,
            "room_block_delete": self.block.pk,
            "booking_detail": self.order.pk,
            "booking_cancel": self.order.pk,
            "booking_resend": self.order.pk,
            "booking_refund_retry": self.order.pk,
            "season_edit": self.season.pk,
            "season_delete": self.season.pk,
            "discount_edit": self.code.pk,
            "invoice_pdf": self.order.invoices.get(kind=InvoiceKind.INVOICE).pk,
            "customer_detail": self.order.pk,
        }
        return reverse(f"dashboard:{name}", args=[args[name]] if name in args else [])

    def dashboard_names(self):
        from maykinbooking.dashboard import urls

        return [pattern.name for pattern in urls.urlpatterns]

    def test_get_of_every_dashboard_url(self):
        reception = self.staff_client(self.reception)
        names = self.dashboard_names()
        self.assertGreaterEqual(len(names), 30)
        for name in names:
            with self.subTest(name=name):
                response = reception.get(self.url(name))
                self.assertEqual(response.status_code, RECEPTION_GET.get(name, 403))
                if name == "booking_export":
                    b"".join(response.streaming_content)
        self.assertRedirects(
            reception.get(self.url("home")),
            reverse("dashboard:booking_list"),
            fetch_redirect_response=False,
        )

    def test_post_to_every_management_url_is_refused_and_changes_nothing(self):
        reception = self.staff_client(self.reception)
        data = {
            "name": "Gehackt",
            "code": "HACK",
            "kind": "percent",
            "value": "100",
            "percent": "100",
            "weekend_percent": "100",
            "long_stay_min_nights": "2",
            "long_stay_percent": "100",
            "start_date": "2027-04-01",
            "end_date": "2027-04-05",
            "valid_from": "2027-01-01",
            "valid_until": "2027-12-31",
            "quantity": "1",
            "reason": "x",
            "inventory": "0",
            "price": "1",
            "confirm": "on",
        }
        for name in self.dashboard_names():
            if name in RECEPTION_GET:
                continue
            with self.subTest(name=name):
                self.assertEqual(reception.post(self.url(name), data).status_code, 403)
        self.assertTrue(Hotel.objects.filter(pk=self.hotel.pk).exists())
        self.room.refresh_from_db()
        self.assertEqual(self.room.inventory, self.inventory)
        self.assertFalse(SeasonPeriod.objects.filter(name="Gehackt").exists())
        self.assertTrue(SeasonPeriod.objects.filter(pk=self.season.pk).exists())
        self.assertEqual(PricingSettings.objects.get(pk=1).weekend_percent, D("0.00"))
        self.assertEqual(Invoice.objects.count(), 1)

    def test_the_reception_cancels_sends_again_and_retries(self):
        reception = self.staff_client(self.reception)
        detail_url = self.url("booking_detail")
        response = reception.get(detail_url)
        self.assertNotContains(
            response,
            reverse("dashboard:invoice_pdf", args=[self.order.invoices.get().pk]),
        )

        with commit_hooks():
            response = reception.post(
                self.url("booking_cancel"), {"reason": "Gast belde af", "confirm": "on"}
            )
        self.assertRedirects(response, detail_url, fetch_redirect_response=False)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.CANCELLED)
        change = self.order.status_changes.get(new_value=OrderStatus.CANCELLED)
        self.assertEqual(
            (change.actor_type, change.actor_user, change.reason),
            (ActorType.STAFF, self.reception, "Gast belde af"),
        )
        credit_note = self.order.invoices.get(kind=InvoiceKind.CREDIT_NOTE)
        self.assertEqual(credit_note.created_by, self.reception)

        mail.outbox.clear()
        # Sending again after the cooldown: the cancellation, with the credit note.
        with freeze_time("2027-03-01T10:00:00Z"):
            response = reception.post(self.url("booking_resend"))
        self.assertEqual(response.status_code, 302)
        (message,) = mail.outbox
        self.assertEqual(message.subject, f"Je boeking #{self.order.pk} is geannuleerd")
        self.assertEqual(
            [name for name, _content, _type in message.attachments],
            [credit_note.filename],
        )
        log = (
            self.order.emails.filter(kind=EmailKind.BOOKING_CANCELLATION)
            .order_by("pk")
            .last()
        )
        self.assertEqual(log.triggered_by, self.reception)

        # Paid at the hotel: nothing to refund.
        response = reception.post(self.url("booking_refund_retry"))
        self.assertIn(
            "Er hoeft voor deze boeking niets teruggestort te worden.",
            [str(m) for m in get_messages(response.wsgi_request)],
        )
        self.assertFalse(Refund.objects.exists())

    def test_the_reception_changes_nothing_in_the_admin(self):
        reception = self.staff_client(self.reception)
        for model in admin.site._registry:
            meta = model._meta
            url = reverse(f"admin:{meta.app_label}_{meta.model_name}_changelist")
            allowed = (meta.app_label, meta.model_name) == ("base", "order")
            with self.subTest(model=meta.label):
                response = reception.get(url)
                self.assertEqual(response.status_code, 200 if allowed else 403)
        # Orders only to look at.
        change_url = reverse("admin:base_order_change", args=[self.order.pk])
        response = reception.post(change_url, {"first_name": "Gehackt"})
        self.assertEqual(response.status_code, 403)
        self.order.refresh_from_db()
        self.assertNotEqual(self.order.first_name, "Gehackt")
        self.assertEqual(
            reception.post(
                reverse("admin:base_order_delete", args=[self.order.pk]),
                {"post": "yes"},
            ).status_code,
            403,
        )

    def test_the_api_has_no_customers_or_money_and_the_reception_writes_nothing(self):
        # E4: customers are not in the API, nor are orders, invoices, payments.
        response = Client().get("/api/", HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {"city", "hotel", "review"})
        hotel = (
            Client()
            .get(f"/api/hotel/{self.hotel.pk}/", HTTP_ACCEPT="application/json")
            .json()
        )
        self.assertEqual(
            set(hotel),
            {
                "url",
                "id",
                "hotel_id",
                "name",
                "image",
                "description",
                "price",
                "is_available",
                "terms",
            },
        )
        reception = self.staff_client(self.reception)
        url = f"/api/hotel/{self.hotel.pk}/"
        self.assertEqual(
            reception.patch(
                url, {"name": "Gehackt"}, content_type="application/json"
            ).status_code,
            403,
        )
        self.assertEqual(reception.delete(url).status_code, 403)
        self.hotel.refresh_from_db()
        self.assertEqual(self.hotel.name, "Hotel de Gracht")


# The webhook


@freeze_time("2027-03-01T09:00:00Z")
class WebhookEdgeCaseTests(EdgeCaseTestCase):
    inventory = 1

    def start_payment(self):
        order = book(
            self.room,
            date(2027, 3, 10),
            date(2027, 3, 12),
            payment_method=PaymentMethod.ONLINE,
        )
        return start_online_payment(order.pk)

    def webhook(self, payment_id):
        with commit_hooks():
            return Client().post(reverse("billing:payment_webhook"), {"id": payment_id})

    def assert_not_confirmed(self, payment, *, paid, currency="EUR", status):
        """
        Never confirmed, no invoice, no confirmation mail; what was paid goes
        back (security advice 8): a refund of ``paid`` with ``status``, and the
        refund mail once it is refunded.
        """
        payment.refresh_from_db()
        order = payment.order
        order.refresh_from_db()
        self.assertEqual(payment.status, PaymentState.ERROR)
        self.assertIn("differs", payment.last_error)
        self.assertNotEqual(order.status, OrderStatus.CONFIRMED)
        self.assertNotEqual(order.payment_status, PaymentStatus.PAID)
        self.assertFalse(Invoice.objects.exists())
        refund = payment.refunds.get()
        self.assertEqual(
            (refund.reason, refund.amount, refund.currency, refund.status),
            (RefundReason.MISMATCH, paid, currency, status),
        )
        expected = (
            [f"Terugbetaling boeking #{order.pk}"]
            if status == RefundState.REFUNDED
            else []
        )
        self.assertEqual([message.subject for message in mail.outbox], expected)

    def test_unknown_id_is_accepted_without_asking_anyone(self):
        payment = self.start_payment()
        with mock.patch("maykinbooking.billing.views.sync_payment") as sync:
            for payment_id in ("tr_unknown123", "fake_tr_0123456789abcdef01234567"):
                with self.subTest(payment_id=payment_id):
                    self.assertEqual(self.webhook(payment_id).status_code, 200)
        sync.assert_not_called()
        payment.refresh_from_db()
        self.assertEqual(payment.status, PaymentState.OPEN)

    def test_malformed_ids_are_refused(self):
        for payment_id in ("", "tr_", "re_abc", "tr_abc;1", "tr_" + "a" * 61, "abc"):
            with self.subTest(payment_id=payment_id):
                self.assertEqual(self.webhook(payment_id).status_code, 400)
        self.assertEqual(
            Client().get(reverse("billing:payment_webhook")).status_code, 405
        )

    def test_other_amount_at_the_provider_is_never_confirmed(self):
        payment = self.start_payment()
        pay_at_provider(payment)
        FakeProviderPayment.objects.filter(pk=payment.provider_id).update(
            amount=D("1.00")
        )
        with self.assertLogs("maykinbooking.billing.services.payments", "ERROR"):
            self.assertEqual(self.webhook(payment.provider_id).status_code, 200)
        self.assert_not_confirmed(payment, paid=D("1.00"), status=RefundState.REFUNDED)
        self.assertIn("niet het bedrag van je boeking", mail.outbox[0].body)
        # Once processed, the webhook does not ask again.
        with mock.patch(
            "maykinbooking.billing.providers.fake.FakeProvider.get_payment"
        ) as get_payment:
            self.assertEqual(self.webhook(payment.provider_id).status_code, 200)
        get_payment.assert_not_called()

    def test_other_currency_at_the_provider_is_never_confirmed(self):
        payment = self.start_payment()
        pay_at_provider(payment)
        FakeProviderPayment.objects.filter(pk=payment.provider_id).update(
            currency="USD"
        )
        with self.assertLogs("maykinbooking.billing.services.payments", "ERROR"):
            self.assertEqual(self.webhook(payment.provider_id).status_code, 200)
        self.assert_not_confirmed(
            payment, paid=D("200.00"), currency="USD", status=RefundState.REFUNDED
        )

    @override_settings(MOLLIE_API_KEY=MOLLIE_TEST_KEY)
    def test_mollie_with_another_amount_is_never_confirmed(self):
        payment = PaymentFactory(
            provider=ProviderName.MOLLIE,
            provider_id="tr_WDqYK6vllg",
            order__room=self.room,
            order__hotel=self.hotel,
            order__start_date=date(2027, 3, 10),
            order__end_date=date(2027, 3, 12),
        )
        self.assertEqual(payment.amount, D("200.00"))
        with requests_mock.Mocker() as mocked, self.assertLogs(
            "maykinbooking.billing.services.payments", "ERROR"
        ):
            mocked.get(
                f"{MOLLIE_API}/payments/tr_WDqYK6vllg",
                json=mollie_payment(status="paid", amount="2.00"),
            )
            refunds_url = f"{MOLLIE_API}/payments/tr_WDqYK6vllg/refunds"
            mocked.get(refunds_url, json=mollie_refund_list())
            mocked.post(refunds_url, json=mollie_refund(amount="2.00"))
            self.assertEqual(self.webhook("tr_WDqYK6vllg").status_code, 200)
            # The payment, then the refund of the 2.00 that came in.
            self.assertEqual(
                [(r.method, r.path) for r in mocked.request_history],
                [
                    ("GET", "/v2/payments/tr_wdqyk6vllg"),
                    ("GET", "/v2/payments/tr_wdqyk6vllg/refunds"),
                    ("POST", "/v2/payments/tr_wdqyk6vllg/refunds"),
                ],
            )
            self.assertEqual(
                mocked.last_request.json()["amount"],
                {"currency": "EUR", "value": "2.00"},
            )
            for request in mocked.request_history:
                self.assertEqual(
                    request.headers["Authorization"], f"Bearer {MOLLIE_TEST_KEY}"
                )
        self.assert_not_confirmed(payment, paid=D("2.00"), status=RefundState.PENDING)

    def test_same_paid_webhook_three_times_confirms_once(self):
        payment = self.start_payment()
        pay_at_provider(payment)
        for _attempt in range(3):
            self.assertEqual(self.webhook(payment.provider_id).status_code, 200)
        payment.order.refresh_from_db()
        self.assertEqual(payment.order.status, OrderStatus.CONFIRMED)
        self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(
            payment.order.status_changes.filter(
                new_value=OrderStatus.CONFIRMED
            ).count(),
            1,
        )


# The fake provider switched off


@freeze_time("2027-03-01T09:00:00Z")
class FakeProviderOffTests(EdgeCaseTestCase):
    def paid_fake_payment(self):
        """A booking paid at the fake provider while it was still on."""
        order = book(
            self.room,
            date(2027, 3, 10),
            date(2027, 3, 12),
            payment_method=PaymentMethod.ONLINE,
        )
        payment = start_online_payment(order.pk)
        pay_at_provider(payment)
        with commit_hooks():
            sync_payment(payment.pk)
        payment.refresh_from_db()
        return payment

    def open_fake_payment(self):
        order = book(
            self.room,
            date(2027, 3, 10),
            date(2027, 3, 12),
            payment_method=PaymentMethod.ONLINE,
        )
        return start_online_payment(order.pk)

    def test_the_payment_page_is_not_found(self):
        payment = self.open_fake_payment()
        page = reverse("billing:fake_checkout", args=[payment.provider_id])
        with override_settings(PAYMENT_FAKE_PROVIDER=False):
            self.assertEqual(self.client.get(page).status_code, 404)
            self.assertEqual(
                self.client.post(page, {"action": "paid"}).status_code, 404
            )
            unknown = reverse("billing:fake_checkout", args=["fake_tr_nope"])
            self.assertEqual(self.client.get(unknown).status_code, 404)
        self.assertEqual(
            FakeProviderPayment.objects.get(pk=payment.provider_id).status,
            PaymentState.OPEN,
        )

    @override_settings(PAYMENT_FAKE_PROVIDER=False)
    def test_the_checkout_offers_only_the_hotel(self):
        self.assertFalse(online_payment_available())
        start, end = date(2027, 3, 10), date(2027, 3, 12)
        url, response, total = self.to_confirm(self.room, start, end)
        self.assertNotContains(response, "Nu online betalen (iDEAL)")
        response = self.confirm(url, total, payment_method="online")
        self.assertContains(response, ONLINE_REFUSED)
        self.assertFalse(Order.objects.exists())
        response = self.confirm(url, total, payment_method="at_hotel")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Order.objects.get().payment_status, PaymentStatus.AT_HOTEL)
        with self.assertRaises(OnlinePaymentUnavailable):
            book(self.room, start, end, payment_method=PaymentMethod.ONLINE)

    def test_return_page_and_webhook_of_an_open_payment(self):
        payment = self.open_fake_payment()
        with override_settings(PAYMENT_FAKE_PROVIDER=False):
            response = self.client.get(
                reverse("billing:payment_return", args=[payment.reference])
            )
            self.assertContains(response, "Je betaling wordt verwerkt")
            with self.assertLogs("maykinbooking.billing.views", "ERROR"):
                self.assertEqual(
                    Client()
                    .post(
                        reverse("billing:payment_webhook"), {"id": payment.provider_id}
                    )
                    .status_code,
                    200,
                )
        payment.refresh_from_db()
        self.assertEqual(payment.status, PaymentState.OPEN)
        self.assertEqual(payment.order.status, OrderStatus.PENDING_PAYMENT)

    def test_refund_without_the_provider_waits_until_it_is_back(self):
        payment = self.paid_fake_payment()
        order = payment.order
        with override_settings(PAYMENT_FAKE_PROVIDER=False), commit_hooks():
            cancel_booking(order.pk, actor=Actor.guest())
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CANCELLED)
        self.assertEqual(order.payment_status, PaymentStatus.PAID)
        self.assertTrue(order.invoices.filter(kind=InvoiceKind.CREDIT_NOTE).exists())
        refund = Refund.objects.get()
        self.assertEqual((refund.status, refund.attempts), (RefundState.ERROR, 1))

        # Back on: the retry of staff refunds it.
        with commit_hooks():
            retry_refund(order.pk, actor=Actor.staff(self.manager))
        refund.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(refund.status, RefundState.REFUNDED)
        self.assertEqual(order.payment_status, PaymentStatus.REFUNDED)
