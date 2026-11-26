"""
QA, phase 2 "Geld": the money flows end to end, through the pages, like a
guest and staff use them.

* Book with a discount code and the weekend surcharge, pay at the hotel, then
  cancel: invoice, credit note, the amounts and the mails with attachments.
* Pay online at the fake provider (weekend and season surcharge).
* A late payment after the hold, with the room taken meanwhile, and after the
  cron expired the order.
* A refund that fails first and then succeeds with the button in the dashboard.
* The revenue in the report is net (credit notes) after all of that.

The amounts are worked out by hand in the comments, not taken from the code.
Time is frozen on Monday 1 March 2027, 10:00 in Amsterdam (VAT NL 21%).
"""
from datetime import date
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.messages import get_messages
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from freezegun import freeze_time

from maykinbooking.accounts.tests.factories import (
    ManagerUserFactory,
    ReceptionUserFactory,
)
from maykinbooking.base.models import (
    City,
    EmailKind,
    EmailLog,
    EmailStatus,
    Hotel,
    Order,
    OrderStatus,
    PaymentStatus,
    Room,
    StatusField,
)
from maykinbooking.base.tests.factories import HotelFactory, OrderFactory

from ..models import (
    FakeProviderRefund,
    Invoice,
    InvoiceKind,
    Payment,
    PaymentState,
    PricingSettings,
    Refund,
    RefundReason,
    RefundState,
)
from ..providers import ProviderError
from ..providers.fake import FakeProvider
from .factories import DiscountCodeFactory, SeasonPeriodFactory
from .money_flows import SELLER, SITE_URL, CheckoutDriver, guest
from .utils import TempMediaMixin, commit_hooks

D = Decimal
NOW = "2027-03-01T09:00:00Z"
# Thursday 11 March to Sunday 14 March 2027: Thursday, Friday and Saturday night.
THU, FRI, SAT, SUN = (date(2027, 3, day) for day in (11, 12, 13, 14))

ALEX = "alex@example.com"
BO = "bo@example.com"


def attachment_names(message):
    return [name for name, _content, _mimetype in message.attachments]


def line_values(invoice):
    return list(
        invoice.lines.order_by("position").values_list(
            "kind", "date", "amount_incl", "amount_excl", "revenue_excl"
        )
    )


@freeze_time(NOW)
@override_settings(
    SITE_URL=SITE_URL,
    MOLLIE_API_KEY="",
    PAYMENT_FAKE_PROVIDER=True,
    PAYMENT_HOLD_MINUTES=15,
    DEFAULT_FROM_EMAIL="boekingen@example.com",
    BOOKING_CONTACT_EMAIL="hulp@example.com",
    BOOKING_LIMIT_PER_IP_PER_HOUR=100,
    BOOKING_LIMIT_PER_EMAIL_PER_DAY=100,
    **SELLER,
)
class MoneyFlowTestCase(CheckoutDriver, TempMediaMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.city = City.objects.create(city_id="AMS", name="Amsterdam")
        cls.hotel = Hotel.objects.create(
            city=cls.city,
            hotel_id="AMS01",
            name="Hotel de Gracht",
            address="Keizersgracht 117",
            is_available=True,
        )
        cls.room = Room.objects.create(
            hotel=cls.hotel,
            room_type="Double",
            price=D("100.00"),
            description="Kamer aan de gracht",
            inventory=1,
        )
        cls.reception = ReceptionUserFactory(username="receptie")
        cls.manager = ManagerUserFactory(username="beheer")
        PricingSettings.load()
        PricingSettings.objects.filter(pk=1).update(weekend_percent=D("20"))

    def setUp(self):
        # The rate limits count in the cache, which all tests share.
        cache.clear()

    def my_booking(self, order, client=None):
        response = (client or self.client).get(
            reverse("bookings:detail", args=[order.token])
        )
        self.assertEqual(response.status_code, 200)
        return response

    def staff_client(self, user):
        client = Client()
        client.force_login(user)
        return client

    def messages_of(self, response):
        return [str(message) for message in get_messages(response.wsgi_request)]


class AtHotelWithCodeAndWeekendTests(MoneyFlowTestCase):
    def test_book_with_code_and_weekend_pay_at_hotel_then_cancel(self):
        code = DiscountCodeFactory(code="VOORJAAR", value=D("10"), max_uses=5)

        # 1. Step 3 shows the breakdown: 100 + 120 + 120 = 340, 10% off = 306.
        url, response, total = self.to_confirm(self.room, THU, SUN, code="voorjaar")
        self.assertEqual(total, "306.00")
        self.assertContains(response, "weekend +20%")
        self.assertContains(response, "Kortingscode VOORJAAR")
        self.assertContains(response, "€ 340,00")
        self.assertContains(response, "€ 34,00")
        self.assertContains(response, "€ 306,00")

        response = self.confirm(url, total, payment_method="at_hotel")
        order = Order.objects.get()
        self.assertRedirects(
            response,
            reverse("bookings:detail", args=[order.token]),
            fetch_redirect_response=False,
        )

        # 2. The order with its snapshot.
        self.assertEqual(order.status, OrderStatus.CONFIRMED)
        self.assertEqual(order.payment_status, PaymentStatus.AT_HOTEL)
        self.assertEqual(order.total_price, D("306.00"))
        self.assertEqual(order.subtotal_price, D("340.00"))
        self.assertEqual(
            (order.discount_code, order.code_discount), ("VOORJAAR", D("34.00"))
        )
        self.assertEqual(order.city, "Amsterdam")
        self.assertEqual(
            list(
                order.nights.order_by("date").values_list(
                    "date", "base_price", "weekend_percent", "price"
                )
            ),
            [
                (THU, D("100.00"), D("0.00"), D("100.00")),
                (FRI, D("100.00"), D("20.00"), D("120.00")),
                (SAT, D("100.00"), D("20.00"), D("120.00")),
            ],
        )
        code.refresh_from_db()
        self.assertEqual(code.times_used, 1)
        self.assertEqual(order.discount_redemption.amount, D("34.00"))
        self.assertFalse(Payment.objects.exists())

        # 3. The invoice: VAT back from the total, 306 / 1.21 = 252.89 + 53.11.
        invoice = order.invoices.get()
        self.assertEqual(invoice.number, "2027-00001")
        self.assertEqual(invoice.kind, InvoiceKind.INVOICE)
        self.assertFalse(invoice.is_test)
        self.assertEqual(
            (
                invoice.total_incl,
                invoice.total_excl,
                invoice.vat_amount,
                invoice.vat_rate,
            ),
            (D("306.00"), D("252.89"), D("53.11"), D("21.00")),
        )
        self.assertEqual(invoice.payment_note, "Te betalen in het hotel.")
        self.assertEqual(
            (invoice.start_date, invoice.end_date, invoice.nights), (THU, SUN, 3)
        )
        self.assertEqual(invoice.customer_name, "Alex de Landgraaf")
        self.assertIn("1015CJ Amsterdam", invoice.customer_address)
        self.assertEqual(invoice.seller_kvk, "12345678")
        # Per night excl. VAT (informative) and the revenue: 252.89 pro rata to
        # 100/120/120 of 340 = 74.38, 89.26 and the rest 89.25.
        self.assertEqual(
            line_values(invoice),
            [
                ("night", THU, D("100.00"), D("82.64"), D("74.38")),
                ("night", FRI, D("120.00"), D("99.17"), D("89.26")),
                ("night", SAT, D("120.00"), D("99.17"), D("89.25")),
                ("discount", None, D("-34.00"), D("-28.10"), D("0.00")),
            ],
        )
        descriptions = list(
            invoice.lines.order_by("position").values_list("description", flat=True)
        )
        self.assertIn("weekend +20%", descriptions[1])
        self.assertEqual(descriptions[3], "Kortingscode VOORJAAR")
        self.assertEqual(
            sum(line[4] for line in line_values(invoice)), invoice.total_excl
        )

        # 4. The confirmation mail with the booking PDF and the invoice.
        (confirmation,) = mail.outbox
        self.assertEqual(confirmation.to, [ALEX])
        self.assertEqual(
            confirmation.subject,
            f"Bevestiging boeking #{order.pk} – Hotel de Gracht",
        )
        self.assertIn("Je betaalt in het hotel bij aankomst.", confirmation.body)
        self.assertIn("Kortingscode VOORJAAR", confirmation.body)
        self.assertIn("Totaal: € 306,00", confirmation.body)
        self.assertIn("basisprijs € 100,00, weekend +20%", confirmation.body)
        self.assertEqual(
            attachment_names(confirmation),
            [f"boeking_{order.pk}.pdf", "factuur_2027-00001.pdf"],
        )
        for _name, content, mimetype in confirmation.attachments:
            self.assertEqual(mimetype, "application/pdf")
            self.assertTrue(content.startswith(b"%PDF"))

        # 5. "Mijn boeking": the same invoice as the attachment.
        response = self.my_booking(order)
        invoice_url = reverse("bookings:invoice", args=[order.token, invoice.pk])
        self.assertContains(response, f'href="{invoice_url}"')
        self.assertContains(response, "€ 306,00")
        download = self.client.get(invoice_url)
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.content, confirmation.attachments[1][1])

        # 6. New price rules leave the booking as it is (snapshot).
        PricingSettings.objects.filter(pk=1).update(weekend_percent=D("50"))
        self.assertContains(self.my_booking(order), "€ 306,00")
        order.refresh_from_db()
        self.assertEqual(order.total_price, D("306.00"))

        # 7. The guest cancels (free until Wednesday 10 March): a credit note of
        # all of it, no refund, the code keeps counting.
        cancel_url = reverse("bookings:cancel", args=[order.token])
        self.assertEqual(self.client.get(cancel_url).status_code, 200)
        with commit_hooks():
            response = self.client.post(cancel_url)
        self.assertRedirects(
            response,
            reverse("bookings:detail", args=[order.token]),
            fetch_redirect_response=False,
        )
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CANCELLED)
        self.assertEqual(order.payment_status, PaymentStatus.AT_HOTEL)
        self.assertFalse(Refund.objects.exists())
        code.refresh_from_db()
        self.assertEqual(code.times_used, 1)

        credit_note = order.invoices.get(kind=InvoiceKind.CREDIT_NOTE)
        self.assertEqual(credit_note.number, "2027-00002")
        self.assertEqual(credit_note.credited_invoice, invoice)
        self.assertEqual(
            (
                credit_note.total_incl,
                credit_note.total_excl,
                credit_note.vat_amount,
                credit_note.vat_rate,
            ),
            (D("-306.00"), D("-252.89"), D("-53.11"), D("21.00")),
        )
        self.assertEqual(
            line_values(credit_note),
            [
                ("night", THU, D("-100.00"), D("-82.64"), D("-74.38")),
                ("night", FRI, D("-120.00"), D("-99.17"), D("-89.26")),
                ("night", SAT, D("-120.00"), D("-99.17"), D("-89.25")),
                ("discount", None, D("34.00"), D("28.10"), D("0.00")),
            ],
        )
        self.assertEqual(credit_note.payment_note, "Er hoeft niets betaald te worden.")
        self.assertFalse(credit_note.is_test)
        # The invoice itself did not change.
        invoice.refresh_from_db()
        self.assertEqual(invoice.total_incl, D("306.00"))

        # 8. The cancellation mail with the credit note.
        self.assertEqual(len(mail.outbox), 2)
        cancellation = mail.outbox[1]
        self.assertEqual(cancellation.subject, f"Je boeking #{order.pk} is geannuleerd")
        self.assertEqual(attachment_names(cancellation), ["creditnota_2027-00002.pdf"])
        self.assertIn("Je hoeft niets te betalen.", cancellation.body)
        self.assertIn("creditnotanummer 2027-00002", cancellation.body)
        self.assertNotIn("teruggestort", cancellation.body)
        credit_note_url = reverse(
            "bookings:invoice", args=[order.token, credit_note.pk]
        )
        download = self.client.get(credit_note_url)
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.content, cancellation.attachments[0][1])
        self.assertEqual(
            list(order.emails.values_list("kind", "status")),
            [
                (EmailKind.BOOKING_CONFIRMATION, EmailStatus.SENT),
                (EmailKind.BOOKING_CANCELLATION, EmailStatus.SENT),
            ],
        )

        # 9. Management: both in the invoice list, and no revenue left.
        manager = self.staff_client(self.manager)
        response = manager.get(reverse("dashboard:invoice_list"))
        self.assertContains(response, "2027-00001")
        self.assertContains(response, "2027-00002")
        response = manager.get(
            reverse("dashboard:report"),
            {"van": "2027-03-01", "tot": "2027-03-31", "hotel": self.hotel.pk},
        )
        (row,) = response.context["rows"]
        self.assertEqual((row.revenue, row.booked), (D("0.00"), 0))


class OnlinePaymentFlowTests(MoneyFlowTestCase):
    def test_pay_online_with_weekend_and_season(self):
        SeasonPeriodFactory(name="Lente", start_date=SAT, end_date=SAT, percent=D("50"))

        # Thursday 100, Friday 120, Saturday 100 x 1.2 x 1.5 = 180: 400.
        payment_page = self.start_online(self.room, THU, SUN)
        order = Order.objects.get()
        payment = Payment.objects.get()
        self.assertEqual(order.status, OrderStatus.PENDING_PAYMENT)
        self.assertEqual(order.payment_status, PaymentStatus.OPEN)
        self.assertEqual(order.total_price, D("400.00"))
        self.assertEqual(payment.amount, D("400.00"))
        self.assertEqual(payment.status, PaymentState.OPEN)
        self.assertFalse(Invoice.objects.exists())
        self.assertEqual(mail.outbox, [])

        # The last room is held: another guest finds nothing for the period.
        _url, response = self.stay_step(self.room, THU, SUN, client=Client())
        self.assertContains(response, "Geen kamers vrij in deze periode.")

        # Back before paying: the page that waits and reloads.
        return_url = reverse("billing:payment_return", args=[payment.reference])
        response = self.client.get(return_url)
        self.assertContains(response, "Je betaling wordt verwerkt")
        self.assertContains(response, 'http-equiv="refresh"')
        self.assertEqual(response["Referrer-Policy"], "no-referrer")

        response = self.pay_and_return(payment_page)
        self.assertRedirects(
            response,
            reverse("bookings:detail", args=[order.token]),
            fetch_redirect_response=False,
        )
        order.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CONFIRMED)
        self.assertEqual(order.payment_status, PaymentStatus.PAID)
        self.assertIsNone(order.hold_expires_at)
        self.assertEqual(payment.status, PaymentState.PAID)
        self.assertEqual(payment.method, "ideal")
        self.assertIsNotNone(payment.paid_at)
        self.assertIsNotNone(payment.processed_at)

        # The invoice after the payment: 400 / 1.21 = 330.58 + 69.42.
        invoice = order.invoices.get()
        self.assertEqual(invoice.number, "2027-00001")
        self.assertEqual(
            (invoice.total_incl, invoice.total_excl, invoice.vat_amount),
            (D("400.00"), D("330.58"), D("69.42")),
        )
        self.assertEqual(invoice.payment_note, "Betaald via iDEAL op 1 maart 2027.")
        saturday = invoice.lines.get(date=SAT)
        self.assertEqual(saturday.amount_incl, D("180.00"))
        self.assertIn("weekend +20%", saturday.description)
        self.assertIn("Lente +50%", saturday.description)
        self.assertEqual(
            sum(invoice.lines.values_list("revenue_excl", flat=True)), D("330.58")
        )

        (confirmation,) = mail.outbox
        self.assertIn("Betaald via iDEAL op 1 maart 2027.", confirmation.body)
        self.assertNotIn("Je betaalt in het hotel", confirmation.body)
        self.assertEqual(
            attachment_names(confirmation),
            [f"boeking_{order.pk}.pdf", "factuur_2027-00001.pdf"],
        )
        response = self.my_booking(order)
        self.assertContains(response, "Je betaling is ontvangen.")
        self.assertContains(response, "Betaald via iDEAL op 1 maart 2027")

        # The webhook afterwards (three times) changes nothing.
        for _attempt in range(3):
            with commit_hooks():
                response = Client().post(
                    reverse("billing:payment_webhook"), {"id": payment.provider_id}
                )
            self.assertEqual(response.status_code, 200)
        self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(EmailLog.objects.count(), 1)

        # The reception sees the payment and the invoice, nothing to retry.
        response = self.staff_client(self.reception).get(
            reverse("dashboard:booking_detail", args=[order.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["payment"], payment)
        self.assertEqual(response.context["invoice"], invoice)
        self.assertFalse(response.context["refund_retry_needed"])


class LatePaymentFlowTests(MoneyFlowTestCase):
    def test_paid_after_the_hold_while_another_guest_took_the_room(self):
        code = DiscountCodeFactory(code="LAAT", value=D("10"), max_uses=1)
        payment_page = self.start_online(self.room, THU, SUN, code="LAAT")
        late = Order.objects.get()
        self.assertEqual(late.total_price, D("306.00"))
        code.refresh_from_db()
        self.assertEqual(code.times_used, 1)

        # 09:16: the hold has passed, another guest books the room at the hotel.
        other_guest = Client()
        with freeze_time("2027-03-01T09:16:00Z"):
            response = self.book_at_hotel(
                self.room, THU, SUN, guest_data=guest(BO), client=other_guest
            )
        other = Order.objects.exclude(pk=late.pk).get()
        self.assertRedirects(
            response,
            reverse("bookings:detail", args=[other.token]),
            fetch_redirect_response=False,
        )
        self.assertEqual(other.status, OrderStatus.CONFIRMED)

        # 09:17: the first guest pays anyway.
        with freeze_time("2027-03-01T09:17:00Z"):
            response = self.pay_and_return(payment_page)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Je betaling wordt teruggestort")
        self.assertContains(response, "€ 306,00")

        late.refresh_from_db()
        self.assertEqual(late.status, OrderStatus.EXPIRED)
        self.assertEqual(late.payment_status, PaymentStatus.REFUNDED)
        self.assertFalse(late.invoices.exists())
        refund = Refund.objects.get()
        self.assertEqual(refund.payment.order, late)
        self.assertEqual(
            (refund.reason, refund.status, refund.amount),
            (RefundReason.LATE_PAYMENT, RefundState.REFUNDED, D("306.00")),
        )
        self.assertEqual(FakeProviderRefund.objects.count(), 1)
        # The code is free again; the other guest keeps the room and its invoice.
        code.refresh_from_db()
        self.assertEqual(code.times_used, 0)
        other.refresh_from_db()
        self.assertEqual(other.status, OrderStatus.CONFIRMED)
        self.assertEqual(
            list(Invoice.objects.values_list("number", "order")),
            [("2027-00001", other.pk)],
        )

        # Mails: the other guest's confirmation, the refund for the late payer.
        self.assertEqual([message.to for message in mail.outbox], [[BO], [ALEX]])
        refund_mail = mail.outbox[1]
        self.assertEqual(refund_mail.subject, f"Terugbetaling boeking #{late.pk}")
        self.assertIn("kwam binnen nadat de reservering was verlopen", refund_mail.body)
        self.assertIn("€ 306,00", refund_mail.body)
        self.assertEqual(refund_mail.attachments, [])
        self.assertEqual(
            list(late.emails.values_list("kind", flat=True)), [EmailKind.PAYMENT_REFUND]
        )
        response = self.my_booking(late)
        self.assertContains(response, "Niet doorgegaan.")
        self.assertContains(response, "Teruggestort op 1 maart 2027: € 306,00")

    def test_paid_after_the_cron_expired_the_order_comes_in_by_webhook(self):
        payment_page = self.start_online(self.room, THU, SUN)
        order = Order.objects.get()
        payment = Payment.objects.get()

        # The cron leaves an order with an open payment alone until 15 minutes
        # after its hold (09:15 + 15): the guest may be paying at the bank.
        with freeze_time("2027-03-01T09:20:00Z"):
            call_command("expire_pending_orders", stdout=StringIO())
            order.refresh_from_db()
            self.assertEqual(order.status, OrderStatus.PENDING_PAYMENT)
        with freeze_time("2027-03-01T09:31:00Z"):
            call_command("expire_pending_orders", stdout=StringIO())
            order.refresh_from_db()
            self.assertEqual(order.status, OrderStatus.EXPIRED)
            # The guest still had the payment page open and pays.
            self.client.post(payment_page, {"action": "paid"})
            with commit_hooks():
                response = Client().post(
                    reverse("billing:payment_webhook"), {"id": payment.provider_id}
                )
        self.assertEqual(response.status_code, 200)

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.EXPIRED)
        self.assertEqual(order.payment_status, PaymentStatus.REFUNDED)
        self.assertFalse(Invoice.objects.exists())
        refund = payment.refunds.get()
        self.assertEqual(
            (refund.reason, refund.status),
            (RefundReason.LATE_PAYMENT, RefundState.REFUNDED),
        )
        (refund_mail,) = mail.outbox
        self.assertEqual(refund_mail.subject, f"Terugbetaling boeking #{order.pk}")

        # Coming back now shows the refund, not the booking.
        response = self.client.get(
            reverse("billing:payment_return", args=[payment.reference])
        )
        self.assertContains(response, "Je betaling wordt teruggestort")
        self.assertEqual(len(mail.outbox), 1)

    def test_paid_after_the_hold_with_the_room_still_free_is_confirmed(self):
        payment_page = self.start_online(self.room, THU, SUN)
        with freeze_time("2027-03-01T09:16:00Z"):
            response = self.pay_and_return(payment_page)
        order = Order.objects.get()
        self.assertRedirects(
            response,
            reverse("bookings:detail", args=[order.token]),
            fetch_redirect_response=False,
        )
        self.assertEqual(order.status, OrderStatus.CONFIRMED)
        self.assertEqual(order.payment_status, PaymentStatus.PAID)
        self.assertEqual(order.invoices.get().total_incl, D("340.00"))
        self.assertFalse(Refund.objects.exists())
        (confirmation,) = mail.outbox
        self.assertEqual(len(confirmation.attachments), 2)


class FailedRefundThenRetryTests(MoneyFlowTestCase):
    def pay_online(self):
        payment_page = self.start_online(self.room, THU, SUN)
        self.pay_and_return(payment_page)
        order = Order.objects.get()
        self.assertEqual(order.payment_status, PaymentStatus.PAID)
        mail.outbox.clear()
        return order

    def cancel_as_guest(self, order):
        with commit_hooks():
            response = self.client.post(reverse("bookings:cancel", args=[order.token]))
        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        return order

    def test_refund_fails_then_the_reception_retries_from_the_dashboard(self):
        order = self.pay_online()
        timeout = ProviderError("Read timed out", retryable=True)
        with mock.patch.object(FakeProvider, "create_refund", side_effect=timeout):
            order = self.cancel_as_guest(order)

        # Cancelled with a credit note, still paid, the refund in error.
        self.assertEqual(order.status, OrderStatus.CANCELLED)
        self.assertEqual(order.payment_status, PaymentStatus.PAID)
        credit_note = order.invoices.get(kind=InvoiceKind.CREDIT_NOTE)
        self.assertEqual(credit_note.total_incl, D("-340.00"))
        self.assertEqual(
            credit_note.payment_note, "Het betaalde bedrag wordt teruggestort."
        )
        refund = Refund.objects.get()
        self.assertEqual(
            (refund.status, refund.attempts, refund.amount),
            (RefundState.ERROR, 1, D("340.00")),
        )
        self.assertIn("Read timed out", refund.error)
        self.assertFalse(FakeProviderRefund.objects.exists())
        # Only the cancellation mail, with the credit note; no refund mail yet.
        (cancellation,) = mail.outbox
        self.assertEqual(attachment_names(cancellation), [credit_note.filename])
        self.assertIn("€ 340,00 wordt teruggestort", cancellation.body)
        # The guest sees that the money is on its way.
        response = self.my_booking(order)
        self.assertContains(response, "€ 340,00 wordt teruggestort.")
        self.assertNotContains(response, "Teruggestort op")

        # The reception sees the failed refund and the button.
        reception = self.staff_client(self.reception)
        detail_url = reverse("dashboard:booking_detail", args=[order.pk])
        response = reception.get(detail_url)
        self.assertTrue(response.context["refund_retry_needed"])
        self.assertContains(response, "terugbetaling mislukt")
        self.assertContains(response, "Terugbetaling opnieuw proberen")

        # The provider works again: the retry refunds the guest.
        retry_url = reverse("dashboard:booking_refund_retry", args=[order.pk])
        with commit_hooks():
            response = reception.post(retry_url)
        self.assertRedirects(response, detail_url, fetch_redirect_response=False)
        refund.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual((refund.status, refund.attempts), (RefundState.REFUNDED, 2))
        self.assertEqual(order.payment_status, PaymentStatus.REFUNDED)
        (fake_refund,) = FakeProviderRefund.objects.all()
        self.assertEqual(
            (fake_refund.id, fake_refund.amount), (refund.provider_id, D("340.00"))
        )
        self.assertEqual(len(mail.outbox), 2)
        refund_mail = mail.outbox[1]
        self.assertEqual(refund_mail.subject, f"Terugbetaling boeking #{order.pk}")
        self.assertEqual(refund_mail.attachments, [])
        self.assertEqual(
            list(
                order.status_changes.filter(field=StatusField.PAYMENT_STATUS)
                .order_by("pk")
                .values_list("new_value", flat=True)
            ),
            [PaymentStatus.OPEN, PaymentStatus.PAID, PaymentStatus.REFUNDED],
        )
        response = reception.get(detail_url)
        self.assertFalse(response.context["refund_retry_needed"])
        self.assertNotContains(response, "Terugbetaling opnieuw proberen")
        self.assertContains(
            self.my_booking(order), "Teruggestort op 1 maart 2027: € 340,00"
        )

        # Pressing the button again refunds nothing twice.
        with commit_hooks():
            response = reception.post(retry_url)
        self.assertIn(
            "Er hoeft voor deze boeking niets teruggestort te worden.",
            self.messages_of(response),
        )
        self.assertEqual(Refund.objects.count(), 1)
        self.assertEqual(FakeProviderRefund.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 2)

    def test_timeout_after_the_provider_made_the_refund_is_taken_over(self):
        order = self.pay_online()
        real_create_refund = FakeProvider.create_refund

        def made_but_answer_lost(provider, *args, **kwargs):
            real_create_refund(provider, *args, **kwargs)
            raise ProviderError("Read timed out", retryable=True)

        with mock.patch.object(FakeProvider, "create_refund", made_but_answer_lost):
            order = self.cancel_as_guest(order)
        refund = Refund.objects.get()
        self.assertEqual(refund.status, RefundState.ERROR)
        self.assertEqual(FakeProviderRefund.objects.count(), 1)

        manager = self.staff_client(self.manager)
        with commit_hooks():
            manager.post(reverse("dashboard:booking_refund_retry", args=[order.pk]))
        refund.refresh_from_db()
        order.refresh_from_db()
        # Taken over, not refunded twice.
        self.assertEqual(refund.status, RefundState.REFUNDED)
        self.assertEqual(FakeProviderRefund.objects.count(), 1)
        self.assertEqual(refund.provider_id, FakeProviderRefund.objects.get().id)
        self.assertEqual(order.payment_status, PaymentStatus.REFUNDED)


class NetRevenueReportTests(MoneyFlowTestCase):
    """
    Room of 121.00 a night (100.00 without 21% VAT), no weekend surcharge.

    March 2027: 180.00 (2 nights with 10% off: 217.80 / 1.21) + 0 (cancelled at
    the hotel) + 0 (paid online, cancelled and refunded) + 200.00 (30 and 31
    March of a stay up to 2 April) = 380.00. April: 100.00 (1 April).

    (No ``setUpTestData`` here: the class-level ``freeze_time`` of the base
    class binds ``setUpClass`` to the base class, so it would never run.)
    """

    def test_revenue_after_bookings_cancellations_and_refunds(self):
        PricingSettings.objects.filter(pk=1).update(weekend_percent=D("0"))
        room = Room.objects.create(
            hotel=self.hotel,
            room_type="Single",
            price=D("121.00"),
            description="Kamer",
            inventory=5,
        )
        DiscountCodeFactory(code="TIEN", value=D("10"))

        # Confirmed, with the code: 242 - 24.20 = 217.80.
        self.book_at_hotel(room, date(2027, 3, 10), date(2027, 3, 12), code="TIEN")
        # Cancelled by the guest.
        self.book_at_hotel(
            room,
            date(2027, 3, 15),
            date(2027, 3, 16),
            guest_data=guest("c@example.com"),
        )
        cancelled = Order.objects.get(start_date=date(2027, 3, 15))
        with commit_hooks():
            self.client.post(reverse("bookings:cancel", args=[cancelled.token]))
        # Paid online, cancelled by the reception, refunded.
        payment_page = self.start_online(
            room,
            date(2027, 3, 20),
            date(2027, 3, 22),
            guest_data=guest("d@example.com"),
        )
        self.pay_and_return(payment_page)
        refunded = Order.objects.get(start_date=date(2027, 3, 20))
        with commit_hooks():
            response = self.staff_client(self.reception).post(
                reverse("dashboard:booking_cancel", args=[refunded.pk]),
                {"reason": "Gast belde af", "confirm": "on"},
            )
        self.assertEqual(response.status_code, 302)
        refunded.refresh_from_db()
        self.assertEqual(refunded.payment_status, PaymentStatus.REFUNDED)
        # Over the end of the month: 363, three nights of 100.00.
        self.book_at_hotel(
            room, date(2027, 3, 30), date(2027, 4, 2), guest_data=guest("e@example.com")
        )
        # A failed online payment: expired, no invoice.
        payment_page = self.start_online(
            room,
            date(2027, 3, 24),
            date(2027, 3, 25),
            guest_data=guest("f@example.com"),
        )
        self.pay_and_return(payment_page, action="failed")
        self.assertEqual(
            Order.objects.get(start_date=date(2027, 3, 24)).status, OrderStatus.EXPIRED
        )
        # From before the invoices: booked, but no revenue.
        OrderFactory(
            hotel=self.hotel,
            room=room,
            start_date=date(2027, 3, 5),
            end_date=date(2027, 3, 6),
        )
        # Another hotel stays out of this hotel's row.
        other_room = Room.objects.create(
            hotel=HotelFactory(city=self.city, is_available=True),
            room_type="Single",
            price=D("121.00"),
            description="Kamer",
            inventory=1,
        )
        self.book_at_hotel(
            other_room,
            date(2027, 3, 10),
            date(2027, 3, 11),
            guest_data=guest("g@example.com"),
        )

        self.assertEqual(Invoice.objects.filter(kind=InvoiceKind.INVOICE).count(), 5)
        self.assertEqual(
            Invoice.objects.filter(kind=InvoiceKind.CREDIT_NOTE).count(), 2
        )

        manager = self.staff_client(self.manager)
        march = manager.get(
            reverse("dashboard:report"),
            {"van": "2027-03-01", "tot": "2027-03-31", "hotel": self.hotel.pk},
        ).context["rows"][0]
        april = manager.get(
            reverse("dashboard:report"),
            {"van": "2027-04-01", "tot": "2027-04-30", "hotel": self.hotel.pk},
        ).context["rows"][0]
        self.assertEqual(march.revenue, D("380.00"))
        self.assertEqual(april.revenue, D("100.00"))
        # Booked nights: 2 + 2 (30, 31 March) + 1 (from before the invoices).
        self.assertEqual(march.booked, 5)
        self.assertEqual(april.booked, 1)
        # Capacity: 1 double + 5 singles, 31 nights.
        self.assertEqual(march.capacity, 6 * 31)

        # The same as everything this hotel invoiced, minus its credit notes.
        invoiced = sum(
            Invoice.objects.filter(order__hotel=self.hotel).values_list(
                "total_excl", flat=True
            ),
            D("0.00"),
        )
        self.assertEqual(march.revenue + april.revenue, invoiced)

        # All hotels: the other hotel adds 100.00 in March.
        response = manager.get(
            reverse("dashboard:report"), {"van": "2027-03-01", "tot": "2027-03-31"}
        )
        self.assertEqual(response.context["total"].revenue, D("480.00"))
        # The tile on the home page: this month (March), all hotels.
        response = manager.get(reverse("dashboard:home"))
        self.assertEqual(response.context["month_revenue"], D("480.00"))
