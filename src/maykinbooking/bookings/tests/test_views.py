"""
"Mijn boeking" (US2.2, US2.3, testpunt 11): the guest's booking page, the PDF
and cancelling, all reached with the token in the URL.
"""
import uuid
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from freezegun import freeze_time

from maykinbooking.base.models import (
    EmailKind,
    EmailLog,
    OrderStatus,
    OrderStatusChange,
    PaymentStatus,
    StatusField,
)
from maykinbooking.base.tests.factories import HotelFactory, OrderFactory, RoomFactory
from maykinbooking.billing.models import (
    InvoiceKind,
    PaymentState,
    PricingSettings,
    RefundReason,
    RefundState,
)
from maykinbooking.billing.services.payments import start_online_payment, sync_payment
from maykinbooking.billing.tests.factories import (
    DiscountCodeFactory,
    PaymentFactory,
    RefundFactory,
)
from maykinbooking.billing.tests.utils import (
    TempMediaMixin,
    book,
    commit_hooks,
    pay_at_provider,
)

from ..services.booking import CancellationNotAllowed, PaymentMethod

# Arrival on Wednesday 10 March 2027: free cancellation until Tuesday 9 March
# 00:00 in Amsterdam (08-03 23:00 UTC), so the last free day is Monday 8 March.
ARRIVAL = date(2027, 3, 10)
BEFORE_DEADLINE = "2027-03-08T22:59:59Z"
AT_DEADLINE = "2027-03-08T23:00:00Z"
LAST_FREE_DAY = "maandag 8 maart"
# The same wording as the mails and the PDF.
CAN_CANCEL = f"Kosteloos annuleren kan tot en met {LAST_FREE_DAY}."
CANNOT_CANCEL = "Kosteloos annuleren kan voor deze boeking niet meer."


@override_settings(BOOKING_CONTACT_EMAIL="hulp@example.com")
class MyBookingTestCase(TempMediaMixin, TestCase):
    def setUp(self):
        # The rate limits count in the cache, which all tests share.
        cache.clear()
        hotel = HotelFactory(
            is_available=True, name="Hotel de Gracht", address="Keizersgracht 117"
        )
        self.room = RoomFactory(hotel=hotel, room_type="Double", price=Decimal("100"))
        self.order = OrderFactory(
            hotel=hotel,
            room=self.room,
            start_date=ARRIVAL,
            end_date=date(2027, 3, 12),
            first_name="Alex",
            last_name="de Landgraaf",
            email="alex@example.com",
        )

    def url(self, name, order=None):
        return reverse(
            f"bookings:{name}", kwargs={"token": (order or self.order).token}
        )

    def assert_private(self, response):
        """Testpunt 11: never cached, never indexed."""
        self.assertIn("no-store", response["Cache-Control"])
        self.assertIn("private", response["Cache-Control"])
        self.assertEqual(response["X-Robots-Tag"], "noindex")
        # Security S6: links on the page never send the URL with the token on.
        self.assertEqual(response["Referrer-Policy"], "no-referrer")


@freeze_time(BEFORE_DEADLINE)
class BookingDetailTests(MyBookingTestCase):
    def test_url(self):
        self.assertEqual(self.url("detail"), f"/boeking/{self.order.token}/")

    def test_unknown_token_is_404(self):
        response = self.client.get(
            reverse("bookings:detail", kwargs={"token": uuid.uuid4()})
        )
        self.assertEqual(response.status_code, 404)

    def test_order_id_is_not_a_token(self):
        response = self.client.get(f"/boeking/{self.order.pk}/")
        self.assertEqual(response.status_code, 404)

    def test_page_is_private(self):
        response = self.client.get(self.url("detail"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "bookings/detail.html")
        self.assertContains(response, '<meta name="robots" content="noindex" />')
        self.assert_private(response)

    def test_shows_the_booking(self):
        response = self.client.get(self.url("detail"))
        self.assertContains(response, "Hotel de Gracht")
        self.assertContains(response, "Keizersgracht 117")
        self.assertContains(response, "Double")
        self.assertContains(response, "Alex de Landgraaf")
        self.assertContains(response, "woensdag 10 maart 2027")
        self.assertContains(response, "vrijdag 12 maart 2027")
        self.assertContains(response, "2 nachten")
        self.assertContains(response, "€ 200,00")
        self.assertContains(response, "Bevestigd")
        self.assertContains(response, "Betalen in het hotel")

    def test_shows_the_prices_of_the_nights_not_the_current_room_price(self):
        self.room.price = Decimal("250")
        self.room.save()
        response = self.client.get(self.url("detail"))
        self.assertContains(response, "€ 100,00", count=2)
        self.assertContains(response, "€ 200,00")
        self.assertNotContains(response, "€ 250")

    def test_booking_number_has_no_thousands_separator(self):
        order = OrderFactory(id=12345, hotel=self.order.hotel, room=self.room)
        response = self.client.get(self.url("detail", order))
        self.assertContains(response, "#12345")
        self.assertNotContains(response, "12.345")

    def test_pdf_can_be_downloaded(self):
        response = self.client.get(self.url("detail"))
        self.assertContains(response, f'href="{self.url("pdf")}"')

    def test_free_cancellation_until_the_last_free_day(self):
        response = self.client.get(self.url("detail"))
        self.assertContains(response, CAN_CANCEL)
        self.assertContains(response, f'href="{self.url("cancel")}"')

    @freeze_time(AT_DEADLINE)
    def test_after_the_deadline_there_is_a_contact_link(self):
        response = self.client.get(self.url("detail"))
        self.assertNotContains(response, f'href="{self.url("cancel")}"')
        self.assertContains(response, CANNOT_CANCEL)
        self.assertContains(response, 'href="mailto:hulp@example.com"')

    def test_cancelled_booking(self):
        self.order.status = OrderStatus.CANCELLED
        self.order.save()
        response = self.client.get(self.url("detail"))
        self.assertContains(response, "Geannuleerd")
        self.assertContains(response, "Deze boeking is geannuleerd.")
        self.assertNotContains(response, f'href="{self.url("cancel")}"')
        self.assertNotContains(response, "Kosteloos annuleren")

    def test_booking_waiting_for_payment(self):
        self.order.status = OrderStatus.PENDING_PAYMENT
        self.order.payment_status = PaymentStatus.OPEN
        self.order.save()
        response = self.client.get(self.url("detail"))
        self.assertContains(response, "Wacht op betaling")
        self.assertNotContains(response, f'href="{self.url("cancel")}"')
        self.assertContains(response, 'href="mailto:hulp@example.com"')

    def test_other_bookings_are_not_shown(self):
        other = OrderFactory(first_name="Sam", hotel=self.order.hotel, room=self.room)
        response = self.client.get(self.url("detail", other))
        self.assertContains(response, "Sam")
        self.assertNotContains(response, "Alex de Landgraaf")


class BookingPDFTests(MyBookingTestCase):
    def test_download(self):
        response = self.client.get(self.url("pdf"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(
            response["Content-Disposition"],
            f'attachment; filename="boeking_{self.order.pk}.pdf"',
        )
        self.assertTrue(response.content.startswith(b"%PDF"))
        self.assert_private(response)

    @mock.patch("maykinbooking.bookings.views.render_order_pdf", return_value=b"%PDF")
    def test_uses_the_same_rendering_as_the_mail(self, render_order_pdf):
        self.client.get(self.url("pdf"))
        render_order_pdf.assert_called_once_with(self.order)

    def test_unknown_token_is_404(self):
        response = self.client.get(
            reverse("bookings:pdf", kwargs={"token": uuid.uuid4()})
        )
        self.assertEqual(response.status_code, 404)

    @override_settings(BOOKING_PDF_LIMIT_PER_IP_PER_MINUTE=3)
    @mock.patch("maykinbooking.bookings.views.render_order_pdf", return_value=b"%PDF")
    def test_downloads_per_ip_address_per_minute(self, render_order_pdf):
        """Security S1: at most 20 PDFs per minute per IP address (here 3)."""
        with freeze_time("2027-03-01T09:00:00Z") as frozen:
            statuses = [self.client.get(self.url("pdf")).status_code for _ in range(4)]
            self.assertEqual(statuses, [200, 200, 200, 429])

            response = self.client.get(self.url("pdf"))
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response["Retry-After"], "60")
            self.assert_private(response)
            self.assertEqual(render_order_pdf.call_count, 3)

            other_ip = Client(REMOTE_ADDR="192.0.2.50")
            self.assertEqual(other_ip.get(self.url("pdf")).status_code, 200)

            frozen.tick(timedelta(seconds=61))
            self.assertEqual(self.client.get(self.url("pdf")).status_code, 200)

    @override_settings(BOOKING_PDF_LIMIT_PER_IP_PER_MINUTE=1)
    @mock.patch("maykinbooking.bookings.views.render_order_pdf", return_value=b"%PDF")
    def test_without_client_ip_there_is_no_limit(self, render_order_pdf):
        client = Client(REMOTE_ADDR="")
        with self.assertLogs("maykinbooking.bookings.throttle", "WARNING") as logs:
            statuses = [client.get(self.url("pdf")).status_code for _ in range(3)]
        self.assertEqual(statuses, [200, 200, 200])
        self.assertIn("booking-pdf without client IP address", logs.output[0])

    @override_settings(BOOKING_PDF_LIMIT_PER_IP_PER_MINUTE=2)
    def test_guessing_tokens_counts_too(self):
        for _ in range(2):
            self.client.get(reverse("bookings:pdf", kwargs={"token": uuid.uuid4()}))
        self.assertEqual(self.client.get(self.url("pdf")).status_code, 429)


@freeze_time(BEFORE_DEADLINE)
class BookingCancelTests(MyBookingTestCase):
    def cancel(self, client=None, order=None):
        with self.captureOnCommitCallbacks(execute=True):
            return (client or self.client).post(self.url("cancel", order))

    def cancellation_logs(self):
        return OrderStatusChange.objects.filter(
            order=self.order,
            field=StatusField.STATUS,
            new_value=OrderStatus.CANCELLED,
        )

    def test_get_asks_for_confirmation(self):
        response = self.client.get(self.url("cancel"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "bookings/cancel.html")
        self.assertContains(response, "Weet je zeker dat je wilt annuleren?")
        self.assertContains(response, CAN_CANCEL)
        self.assertContains(response, 'method="post"')
        self.assertContains(response, '<meta name="robots" content="noindex" />')
        self.assert_private(response)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.CONFIRMED)

    def test_post_cancels_the_booking(self):
        response = self.cancel()

        self.assertRedirects(
            response, self.url("detail"), fetch_redirect_response=False
        )
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.CANCELLED)
        log = self.cancellation_logs().get()
        self.assertEqual(log.actor_type, "guest")
        self.assertIsNone(log.actor_user)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(
            EmailLog.objects.get(order=self.order).kind,
            EmailKind.BOOKING_CANCELLATION,
        )

        response = self.client.get(response.url)
        self.assertContains(response, "Je boeking is geannuleerd.")
        self.assertContains(response, "Deze boeking is geannuleerd.")

    def test_cancelling_twice_changes_nothing(self):
        self.cancel()
        response = self.cancel()

        self.assertRedirects(
            response, self.url("detail"), fetch_redirect_response=False
        )
        self.assertEqual(self.cancellation_logs().count(), 1)
        self.assertEqual(len(mail.outbox), 1)
        response = self.client.get(response.url)
        self.assertContains(response, "Deze boeking was al geannuleerd.")

    def test_cancelled_booking_shows_no_button(self):
        self.cancel()
        response = self.client.get(self.url("cancel"))
        self.assertContains(response, "Deze boeking is al geannuleerd")
        self.assertNotContains(response, 'method="post"')

    @freeze_time(AT_DEADLINE)
    def test_get_after_the_deadline_explains_and_shows_the_contact(self):
        response = self.client.get(self.url("cancel"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, CANNOT_CANCEL)
        self.assertContains(response, 'href="mailto:hulp@example.com"')
        self.assertNotContains(response, 'method="post"')

    @freeze_time(AT_DEADLINE)
    def test_post_after_the_deadline_is_refused(self):
        response = self.cancel()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'role="alert"')
        self.assertContains(response, CANNOT_CANCEL)
        self.assertContains(response, 'href="mailto:hulp@example.com"')
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.CONFIRMED)
        self.assertFalse(self.cancellation_logs().exists())
        self.assertEqual(len(mail.outbox), 0)

    @mock.patch(
        "maykinbooking.bookings.views.cancel_booking",
        side_effect=CancellationNotAllowed("Te laat"),
    )
    def test_cancellation_not_allowed_is_explained(self, cancel_booking):
        response = self.cancel()
        cancel_booking.assert_called_once()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'href="mailto:hulp@example.com"')
        self.assertNotContains(response, 'method="post"')

    def test_expired_booking_cannot_be_cancelled(self):
        self.order.status = OrderStatus.EXPIRED
        self.order.save()
        response = self.cancel()
        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.EXPIRED)

    def test_unknown_token_is_404(self):
        url = reverse("bookings:cancel", kwargs={"token": uuid.uuid4()})
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url).status_code, 404)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.CONFIRMED)

    def test_post_needs_a_csrf_token(self):
        response = self.cancel(client=Client(enforce_csrf_checks=True))
        self.assertEqual(response.status_code, 403)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.CONFIRMED)

    def test_only_the_booking_of_the_token_is_cancelled(self):
        other = OrderFactory(
            hotel=self.order.hotel,
            room=self.room,
            start_date=ARRIVAL,
            end_date=date(2027, 3, 11),
        )
        self.cancel(order=other)
        other.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(other.status, OrderStatus.CANCELLED)
        self.assertEqual(self.order.status, OrderStatus.CONFIRMED)


@freeze_time(BEFORE_DEADLINE)
@override_settings(
    MOLLIE_API_KEY="",
    PAYMENT_FAKE_PROVIDER=True,
    SITE_URL="https://booking.example.com",
)
class MyBookingMoneyTests(MyBookingTestCase):
    """Phase 2: what was paid, what goes back, the discounts and the invoices."""

    def book(self, **kwargs):
        with commit_hooks():
            return book(self.room, ARRIVAL, date(2027, 3, 12), **kwargs)

    def book_and_pay(self):
        order = self.book(payment_method=PaymentMethod.ONLINE)
        payment = start_online_payment(order.pk)
        pay_at_provider(payment)
        with commit_hooks():
            sync_payment(payment.pk)
        order.refresh_from_db()
        return order

    def test_paying_at_the_hotel(self):
        order = self.book()
        invoice = order.invoices.get()
        response = self.client.get(self.url("detail", order))
        self.assertContains(response, "Je betaalt in het hotel bij aankomst.")
        self.assertNotContains(response, "Betaald via iDEAL")
        self.assertNotContains(response, "teruggestort")
        self.assertContains(response, f"Download factuur {invoice.number} (PDF)")
        self.assertContains(
            response,
            f'href="{reverse("bookings:invoice", args=[order.token, invoice.pk])}"',
        )
        self.assertNotContains(response, "creditnota")

    def test_paid_online(self):
        order = self.book_and_pay()
        self.assertEqual(order.payment_status, PaymentStatus.PAID)
        response = self.client.get(self.url("detail", order))
        self.assertContains(response, "Betaald via iDEAL op 8 maart 2027")
        self.assertNotContains(response, "Je betaalt in het hotel")
        self.assertContains(response, "Download factuur")
        # Cancelling for free gives the money back.
        self.assertContains(response, "Je krijgt dan € 200,00 teruggestort.")

    def test_order_from_before_invoicing_has_no_invoice_link(self):
        response = self.client.get(self.url("detail"))
        self.assertNotContains(response, "Download factuur")
        self.assertContains(response, "Download bevestiging (PDF)")

    def refund(self, **kwargs):
        """A refund of € 200,00 of a cancelled booking that was paid online."""
        return RefundFactory(
            payment__order__hotel=self.order.hotel,
            payment__order__room=self.room,
            payment__order__start_date=ARRIVAL,
            payment__order__end_date=date(2027, 3, 12),
            payment__paid_at=datetime(2027, 3, 1, 9, tzinfo=dt_timezone.utc),
            **kwargs,
        )

    def test_refund_on_its_way(self):
        refund = self.refund(status=RefundState.PENDING)
        order = refund.payment.order
        response = self.client.get(self.url("detail", order))
        self.assertContains(response, "Betaald via iDEAL op 1 maart 2027")
        self.assertContains(response, "€ 200,00 wordt teruggestort.")
        self.assertContains(response, "Dat duurt meestal 1 tot 3 werkdagen.")
        self.assertContains(response, "het betaalde bedrag wordt teruggestort")
        self.assertNotContains(response, "je betaalt niets")

    def test_failed_refund_is_still_on_its_way_for_the_guest(self):
        refund = self.refund(status=RefundState.ERROR, attempts=2)
        response = self.client.get(self.url("detail", refund.payment.order))
        self.assertContains(response, "€ 200,00 wordt teruggestort.")

    def test_refunded(self):
        refund = self.refund(
            status=RefundState.REFUNDED,
            refunded_at=datetime(2027, 3, 2, 12, tzinfo=dt_timezone.utc),
            payment__order__payment_status=PaymentStatus.REFUNDED,
        )
        response = self.client.get(self.url("detail", refund.payment.order))
        self.assertContains(response, "Teruggestort op 2 maart 2027: € 200,00")
        self.assertNotContains(response, "wordt teruggestort.")

    def test_duplicate_payment_going_back(self):
        refund = self.refund(
            reason=RefundReason.DUPLICATE,
            status=RefundState.PENDING,
            payment__order__status=OrderStatus.CONFIRMED,
        )
        response = self.client.get(self.url("detail", refund.payment.order))
        self.assertContains(response, "Je had twee keer betaald.")

    def test_price_breakdown_with_surcharges_and_discounts(self):
        PricingSettings.load()
        PricingSettings.objects.filter(pk=1).update(weekend_percent=Decimal("20"))
        DiscountCodeFactory(code="WELKOM", value=Decimal("10"))
        # Thursday 11 to Sunday 14 March: two weekend nights.
        with commit_hooks():
            order = book(
                self.room,
                date(2027, 3, 11),
                date(2027, 3, 14),
                discount_code="WELKOM",
            )
        self.assertEqual(order.total_price, Decimal("306.00"))

        response = self.client.get(self.url("detail", order))
        self.assertContains(response, "weekend +20%", count=2)
        self.assertContains(response, "€ 120,00", count=2)
        self.assertContains(response, "Subtotaal")
        self.assertContains(response, "€ 340,00")
        self.assertContains(response, "Kortingscode WELKOM")
        self.assertContains(response, "− € 34,00")
        self.assertContains(response, "€ 306,00")

    def test_waiting_for_payment_links_back_to_the_payment_page(self):
        payment = PaymentFactory(
            order__hotel=self.order.hotel,
            order__room=self.room,
            order__start_date=ARRIVAL,
            order__end_date=date(2027, 3, 12),
            order__hold_expires_at=datetime(2027, 3, 8, 23, 14, tzinfo=dt_timezone.utc),
            checkout_url="https://www.mollie.com/checkout/select-method/abc",
        )
        response = self.client.get(self.url("detail", payment.order))
        self.assertContains(response, "We wachten op je betaling.")
        self.assertContains(response, "De kamer is tot 00:14 uur voor je gereserveerd.")
        self.assertContains(
            response,
            'href="https://www.mollie.com/checkout/select-method/abc" rel="noreferrer"',
        )

    def test_payment_page_on_another_host_is_never_linked(self):
        """Security: only the hosts the payment service allows."""
        payment = PaymentFactory(checkout_url="https://evil.example/pay")
        response = self.client.get(self.url("detail", payment.order))
        self.assertContains(response, "We wachten op je betaling.")
        self.assertNotContains(response, "evil.example")
        self.assertNotContains(response, "Ga verder met betalen")

    def test_finished_payment_is_not_offered_again(self):
        payment = PaymentFactory(
            status=PaymentState.CANCELED,
            checkout_url="https://www.mollie.com/checkout/select-method/abc",
        )
        response = self.client.get(self.url("detail", payment.order))
        self.assertNotContains(response, "Ga verder met betalen")

    def test_expired_booking(self):
        self.order.status = OrderStatus.EXPIRED
        self.order.payment_status = PaymentStatus.OPEN
        self.order.save()
        response = self.client.get(self.url("detail"))
        self.assertContains(response, "Niet doorgegaan.")
        self.assertContains(
            response, f'href="{reverse("order", args=[self.order.hotel.pk])}"'
        )
        self.assertNotContains(response, 'id="cancel-title"')

    def test_cancel_page_of_a_paid_booking_mentions_the_refund(self):
        order = self.book_and_pay()
        response = self.client.get(self.url("cancel", order))
        self.assertContains(response, "Weet je zeker dat je wilt annuleren?")
        self.assertContains(response, "Je krijgt € 200,00 teruggestort.")

    def test_cancel_page_when_paying_at_the_hotel_is_unchanged(self):
        response = self.client.get(self.url("cancel"))
        self.assertContains(response, "Weet je zeker dat je wilt annuleren?")
        self.assertNotContains(response, "teruggestort")

    def test_cancelling_a_paid_booking_refunds_it(self):
        order = self.book_and_pay()
        mail.outbox = []
        with commit_hooks():
            response = self.client.post(self.url("cancel", order))

        response = self.client.get(response["Location"])
        self.assertContains(
            response,
            "Je boeking is geannuleerd. Je krijgt € 200,00 teruggestort en een "
            "bevestiging per e-mail.",
        )
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CANCELLED)
        # The fake provider refunds at once.
        self.assertEqual(order.payment_status, PaymentStatus.REFUNDED)
        self.assertContains(response, "Teruggestort op 8 maart 2027: € 200,00")
        credit_note = order.invoices.get(kind=InvoiceKind.CREDIT_NOTE)
        self.assertContains(response, f"Download creditnota {credit_note.number} (PDF)")
        self.assertContains(
            response, reverse("bookings:invoice", args=[order.token, credit_note.pk])
        )
