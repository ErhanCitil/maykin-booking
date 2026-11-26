"""
End-to-end flows through the test client: US1.2, US1.3, US2.1-US2.3, US3.1-US3.3,
and the money of phase 2.

A guest searches the city page with a period, opens the hotel, goes through the
three checkout steps, gets the confirmation mail with the PDF, the invoice and
the link, opens "Mijn boeking" with that link, downloads the PDF and the invoice
and cancels. Afterwards the room is free again on the city page and staff sees
the cancelled booking with its log in the dashboard.

Phase 2: paying online at the fake provider (paid, failed), a discount code,
a price that changes before confirming, the invoice of another booking, and
cancelling a paid booking (refund and credit note).

Time is frozen in UTC; mails are sent with ``captureOnCommitCallbacks`` or, when
a callback registers another one (a refund sends its mail), ``commit_hooks``.
"""
import re
from datetime import date
from decimal import Decimal
from urllib.parse import urlencode

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils.html import escape

from freezegun import freeze_time

from maykinbooking.accounts.tests.factories import ManagerUserFactory
from maykinbooking.base.models import (
    ActorType,
    City,
    EmailKind,
    EmailLog,
    EmailStatus,
    Hotel,
    Order,
    OrderStatus,
    PaymentStatus,
    Room,
)
from maykinbooking.billing.models import (
    Invoice,
    InvoiceKind,
    Payment,
    PaymentState,
    PricingSettings,
    RefundState,
)
from maykinbooking.billing.tests.factories import DiscountCodeFactory
from maykinbooking.billing.tests.utils import (
    TempMediaMixin,
    commit_hooks,
    fake_checkout_path,
    path_of,
)

SITE_URL = "https://booking.example.com"
# Monday 1 March 2027, 10:00 in Amsterdam.
NOW = "2027-03-01T09:00:00Z"
LATER = "2027-03-01T09:05:00Z"
# Wednesday 10 March to Friday 12 March: free cancellation up to and including
# Monday 8 March.
PERIOD = {"van": "2027-03-10", "tot": "2027-03-12"}
STAY = {"start_date": "2027-03-10", "end_date": "2027-03-12"}
# Arrival tomorrow: the free cancellation deadline (today 00:00) has passed.
LATE_PERIOD = {"van": "2027-03-02", "tot": "2027-03-04"}
LATE_STAY = {"start_date": "2027-03-02", "end_date": "2027-03-04"}

GUEST = {
    "first_name": "Alex",
    "last_name": "de Landgraaf",
    "email": "alex@example.com",
    "address": "Keizersgracht 117",
    "zipcode": "1015 CJ",
    "city": "Amsterdam",
    "country": "NL",
}

MY_BOOKING_LINK = re.compile(re.escape(SITE_URL) + r"(/boeking/[0-9a-f-]{36}/)")


@freeze_time(NOW)
@override_settings(
    SITE_URL=SITE_URL,
    DEFAULT_FROM_EMAIL="boekingen@example.com",
    BOOKING_CONTACT_EMAIL="hulp@example.com",
)
class EndToEndTestCase(TempMediaMixin, TestCase):
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
            price=Decimal("95.50"),
            description="Kamer aan de gracht",
            inventory=1,
        )
        cls.other_hotel = Hotel.objects.create(
            city=cls.city, hotel_id="AMS02", name="Hotel Centraal", is_available=True
        )
        Room.objects.create(
            hotel=cls.other_hotel,
            room_type="Single",
            price=Decimal("70"),
            description="Kamer",
            inventory=1,
        )
        cls.staff = ManagerUserFactory(
            username="medewerker", first_name="Mila", last_name="de Vries"
        )
        cls.city_url = reverse("stad", args=[cls.city.name])

    def setUp(self):
        # The rate limits count in the cache, which all tests share.
        cache.clear()

    # Helpers

    def hotel_names(self, period):
        response = self.client.get(self.city_url, period)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'role="alert"')
        return [hotel.name for hotel in response.context["object_list"]]

    def with_period(self, url, period):
        return f"{url}?{urlencode(period)}"

    def post_step(self, url, step, data, **extra):
        payload = {"checkout-current_step": step}
        payload.update({f"{step}-{key}": value for key, value in data.items()})
        return self.client.post(url, payload, **extra)

    def checkout(self, period, stay, room=None, **extra):
        """Go through the three steps; the mails of the commit are sent."""
        url = self.with_period(reverse("order", args=[self.hotel.pk]), period)
        response = self.client.get(url, **extra)
        self.assertEqual(response.status_code, 200)
        response = self.post_step(
            url, "stay", {**stay, "room": (room or self.room).pk}, **extra
        )
        self.assertContains(response, "Wie verblijft er?")
        response = self.post_step(url, "guest", GUEST, **extra)
        self.assertContains(response, "Klopt alles?")
        with self.captureOnCommitCallbacks(execute=True):
            return self.post_step(url, "confirm", {"terms": "on"}, **extra)

    def my_booking_path(self, message):
        match = MY_BOOKING_LINK.search(message.body)
        self.assertIsNotNone(match, message.body)
        html, mimetype = message.alternatives[0]
        self.assertEqual(mimetype, "text/html")
        self.assertIn(SITE_URL + match.group(1), html)
        return match.group(1)

    def assert_private(self, response):
        self.assertEqual(response["X-Robots-Tag"], "noindex")
        self.assertIn("no-store", response["Cache-Control"])
        # The token must not leak through the Referer header, not even to pages
        # of this site (which could pass it on, e.g. to Sentry or analytics).
        self.assertEqual(response["Referrer-Policy"], "no-referrer")

    def log(self, order):
        return [
            (change.field, change.old_value, change.new_value, change.actor_type)
            for change in order.status_changes.all()
        ]


class GuestBooksAndCancelsTests(EndToEndTestCase):
    def test_search_book_mail_my_booking_pdf_cancel_and_dashboard(self):
        # 1. The city page lists both hotels for the period.
        self.assertEqual(
            self.hotel_names(PERIOD), ["Hotel Centraal", "Hotel de Gracht"]
        )
        response = self.client.get(self.city_url, PERIOD)
        hotel_url = self.with_period(
            reverse("hotel_detail", args=[self.hotel.pk]), PERIOD
        )
        self.assertContains(response, f'href="{escape(hotel_url)}"')

        # 2. The hotel page offers the room with a book link for the period.
        response = self.client.get(hotel_url)
        order_url = self.with_period(reverse("order", args=[self.hotel.pk]), PERIOD)
        self.assertContains(response, f'href="{escape(order_url)}"')
        self.assertNotContains(response, "Niet vrij in deze periode")

        # 3. The checkout starts with the dates of the search.
        response = self.client.get(order_url)
        self.assertContains(response, 'value="2027-03-10"')
        self.assertContains(response, 'value="2027-03-12"')
        self.assertContains(response, f'value="{self.room.pk}"')
        response = self.post_step(order_url, "stay", {**STAY, "room": self.room.pk})
        self.assertContains(response, "€ 191,00")
        response = self.post_step(order_url, "guest", GUEST)
        self.assertContains(response, "Klopt alles?")
        self.assertFalse(Order.objects.exists())
        with self.captureOnCommitCallbacks(execute=True):
            response = self.post_step(order_url, "confirm", {"terms": "on"})

        order = Order.objects.get()
        self.assertEqual(order.status, OrderStatus.CONFIRMED)
        self.assertEqual(order.payment_status, PaymentStatus.AT_HOTEL)
        self.assertEqual(order.total_price, Decimal("191.00"))
        self.assertEqual(
            list(order.nights.values_list("date", "price")),
            [
                (date(2027, 3, 10), Decimal("95.50")),
                (date(2027, 3, 11), Decimal("95.50")),
            ],
        )

        # 4. One confirmation mail, with the PDF and the link to "Mijn boeking".
        self.assertEqual(len(mail.outbox), 1)
        confirmation = mail.outbox[0]
        self.assertEqual(confirmation.to, ["alex@example.com"])
        self.assertEqual(confirmation.from_email, "boekingen@example.com")
        self.assertIn(f"#{order.pk}", confirmation.subject)
        self.assertIn("Hotel de Gracht", confirmation.subject)
        # The booking and the invoice (payment at the hotel: issued at once).
        self.assertEqual(
            [filename for filename, _content, _mimetype in confirmation.attachments],
            [f"boeking_{order.pk}.pdf", "factuur_2027-00001.pdf"],
        )
        for _filename, content, mimetype in confirmation.attachments:
            self.assertEqual(mimetype, "application/pdf")
            self.assertTrue(content.startswith(b"%PDF"))
        link = self.my_booking_path(confirmation)
        self.assertEqual(
            link, reverse("bookings:detail", kwargs={"token": order.token})
        )
        self.assertRedirects(response, link, fetch_redirect_response=False)
        confirmation_log = EmailLog.objects.get()
        self.assertEqual(confirmation_log.kind, EmailKind.BOOKING_CONFIRMATION)
        self.assertEqual(confirmation_log.status, EmailStatus.SENT)

        # 5. The link of the mail opens "Mijn boeking".
        response = self.client.get(link)
        self.assertContains(response, "Je boeking staat vast")
        self.assertContains(response, "Hotel de Gracht")
        self.assertContains(response, "€ 191,00")
        self.assertContains(
            response, "Kosteloos annuleren kan tot en met maandag 8 maart."
        )
        self.assert_private(response)

        # 6. The PDF can be downloaded from there, and so can the invoice: the
        # same file as the attachment.
        pdf_url = reverse("bookings:pdf", kwargs={"token": order.token})
        self.assertContains(response, f'href="{pdf_url}"')
        self.assertContains(response, "Je betaalt in het hotel bij aankomst.")
        response = self.client.get(pdf_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn(
            f'filename="boeking_{order.pk}.pdf"', response["Content-Disposition"]
        )
        self.assertTrue(response.content.startswith(b"%PDF"))
        self.assert_private(response)

        invoice = order.invoices.get()
        invoice_url = reverse("bookings:invoice", args=[order.token, invoice.pk])
        self.assertContains(self.client.get(link), f'href="{invoice_url}"')
        response = self.client.get(invoice_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Content-Disposition"],
            'attachment; filename="factuur_2027-00001.pdf"',
        )
        self.assertEqual(response.content, confirmation.attachments[1][1])
        self.assert_private(response)

        # 7. The room is taken: the hotel is gone from the city page.
        self.assertEqual(self.hotel_names(PERIOD), ["Hotel Centraal"])
        response = self.client.get(hotel_url)
        self.assertContains(response, "Niet vrij in deze periode")
        response = self.client.get(order_url)
        self.assertContains(response, "Geen kamers vrij in deze periode.")

        # 8. The guest cancels: confirmation page, then POST.
        cancel_url = reverse("bookings:cancel", kwargs={"token": order.token})
        response = self.client.get(link)
        self.assertContains(response, f'href="{cancel_url}"')
        response = self.client.get(cancel_url)
        self.assertContains(response, "Weet je zeker dat je wilt annuleren?")
        self.assert_private(response)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(cancel_url)
        self.assertRedirects(response, link, fetch_redirect_response=False)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CANCELLED)

        self.assertEqual(len(mail.outbox), 2)
        cancellation = mail.outbox[1]
        self.assertEqual(cancellation.to, ["alex@example.com"])
        self.assertIn("geannuleerd", cancellation.subject)
        # The credit note of the invoice.
        [(filename, content, mimetype)] = cancellation.attachments
        self.assertEqual(filename, "creditnota_2027-00002.pdf")
        self.assertTrue(content.startswith(b"%PDF"))
        self.assertEqual(self.my_booking_path(cancellation), link)

        response = self.client.get(link)
        self.assertContains(response, "Je boeking is geannuleerd.")
        self.assertContains(response, "Deze boeking is geannuleerd.")
        self.assertNotContains(response, f'href="{cancel_url}"')
        # Nothing was paid, so nothing goes back; the credit note is there.
        self.assertNotContains(response, "teruggestort")
        credit_note = order.invoices.get(kind=InvoiceKind.CREDIT_NOTE)
        credit_note_url = reverse(
            "bookings:invoice", args=[order.token, credit_note.pk]
        )
        self.assertContains(response, f'href="{credit_note_url}"')
        self.assertEqual(self.client.get(credit_note_url).content, content)

        # 9. The room is free again.
        self.assertEqual(
            self.hotel_names(PERIOD), ["Hotel Centraal", "Hotel de Gracht"]
        )
        response = self.client.get(order_url)
        self.assertNotContains(response, "Geen kamers vrij in deze periode.")
        self.assertContains(response, f'value="{self.room.pk}"')

        # 10. Staff sees the cancelled booking, its log and its mails.
        self.client.force_login(self.staff)
        response = self.client.get(
            reverse("dashboard:booking_list"), {"status": OrderStatus.CANCELLED}
        )
        self.assertEqual(list(response.context["orders"]), [order])
        response = self.client.get(
            reverse("dashboard:booking_list"), {"q": "alex@example.com"}
        )
        self.assertEqual(list(response.context["orders"]), [order])
        self.assertContains(response, "Geannuleerd")

        response = self.client.get(reverse("dashboard:booking_detail", args=[order.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [(row["old"], row["new"]) for row in response.context["status_changes"]],
            [
                ("", "Bevestigd"),
                ("", "Betalen in het hotel"),
                ("Bevestigd", "Geannuleerd"),
            ],
        )
        self.assertEqual(
            {change.actor_type for change in order.status_changes.all()},
            {ActorType.GUEST},
        )
        self.assertEqual(
            list(order.emails.values_list("kind", "status")),
            [
                (EmailKind.BOOKING_CONFIRMATION, EmailStatus.SENT),
                (EmailKind.BOOKING_CANCELLATION, EmailStatus.SENT),
            ],
        )
        self.assertContains(response, "Boekingsbevestiging")
        self.assertContains(response, "Annulering")
        self.assertContains(response, SITE_URL + link)

    @override_settings(ALLOWED_HOSTS=["testserver", "evil.example"])
    def test_mail_link_never_comes_from_the_host_header(self):
        self.checkout(PERIOD, STAY, HTTP_HOST="evil.example")

        [confirmation] = mail.outbox
        self.assertNotIn("evil.example", confirmation.body)
        self.assertNotIn("evil.example", confirmation.alternatives[0][0])
        self.my_booking_path(confirmation)


class StaffCancelsAfterTheDeadlineTests(EndToEndTestCase):
    """US3.3: staff can cancel after the guest's deadline, with the same effects."""

    def test_guest_is_refused_and_staff_cancels_with_a_reason(self):
        response = self.checkout(LATE_PERIOD, LATE_STAY)
        order = Order.objects.get()
        link = self.my_booking_path(mail.outbox[0])
        self.assertRedirects(response, link, fetch_redirect_response=False)
        self.assertEqual(self.hotel_names(LATE_PERIOD), ["Hotel Centraal"])

        # The guest has no cancel button any more and a POST is refused.
        cancel_url = reverse("bookings:cancel", kwargs={"token": order.token})
        response = self.client.get(link)
        self.assertNotContains(response, f'href="{cancel_url}"')
        self.assertContains(
            response, "Kosteloos annuleren kan voor deze boeking niet meer."
        )
        self.assertContains(response, 'href="mailto:hulp@example.com"')
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(cancel_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'role="alert"')
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CONFIRMED)
        self.assertEqual(len(mail.outbox), 1)

        # Staff needs a reason and a confirmation.
        self.client.force_login(self.staff)
        staff_cancel_url = reverse("dashboard:booking_cancel", args=[order.pk])
        response = self.client.get(staff_cancel_url)
        self.assertEqual(response.status_code, 200)
        response = self.client.post(staff_cancel_url, {"reason": "", "confirm": "on"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors["reason"])
        response = self.client.post(staff_cancel_url, {"reason": "Gast belde, ziek"})
        self.assertTrue(response.context["form"].errors["confirm"])
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CONFIRMED)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                staff_cancel_url, {"reason": "Gast belde, ziek", "confirm": "on"}
            )
        detail_url = reverse("dashboard:booking_detail", args=[order.pk])
        self.assertRedirects(response, detail_url, fetch_redirect_response=False)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CANCELLED)

        # The same effects as a cancellation by the guest (US2.3) ...
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("geannuleerd", mail.outbox[1].subject)
        self.assertEqual(self.my_booking_path(mail.outbox[1]), link)
        self.assertEqual(
            self.hotel_names(LATE_PERIOD), ["Hotel Centraal", "Hotel de Gracht"]
        )
        self.client.logout()
        response = self.client.get(link)
        self.assertContains(response, "Deze boeking is geannuleerd.")

        # ... and the log has the staff member and the reason.
        self.assertEqual(
            self.log(order),
            [
                ("status", "", OrderStatus.CONFIRMED, ActorType.GUEST),
                ("payment_status", "", PaymentStatus.AT_HOTEL, ActorType.GUEST),
                (
                    "status",
                    OrderStatus.CONFIRMED,
                    OrderStatus.CANCELLED,
                    ActorType.STAFF,
                ),
            ],
        )
        staff_change = order.status_changes.last()
        self.assertEqual(staff_change.actor_user, self.staff)
        self.assertEqual(staff_change.actor_label, "Mila de Vries")
        self.assertEqual(staff_change.reason, "Gast belde, ziek")
        cancellation_log = order.emails.get(kind=EmailKind.BOOKING_CANCELLATION)
        self.assertEqual(cancellation_log.triggered_by, self.staff)

        # Sending it again gives a new log with the staff member (after the
        # cooldown of the dashboard against sending twice by accident).
        self.client.force_login(self.staff)
        with freeze_time(LATER):
            response = self.client.post(
                reverse("dashboard:booking_resend", args=[order.pk])
            )
        self.assertRedirects(response, detail_url, fetch_redirect_response=False)
        self.assertEqual(len(mail.outbox), 3)
        self.assertEqual(
            list(order.emails.values_list("kind", "status", "triggered_by")),
            [
                (EmailKind.BOOKING_CONFIRMATION, EmailStatus.SENT, None),
                (EmailKind.BOOKING_CANCELLATION, EmailStatus.SENT, self.staff.pk),
                (EmailKind.BOOKING_CANCELLATION, EmailStatus.SENT, self.staff.pk),
            ],
        )
        response = self.client.get(detail_url)
        self.assertContains(response, "Gast belde, ziek")
        self.assertContains(response, "Mila de Vries")
        self.assertContains(response, "door medewerker", count=2)


@override_settings(MOLLIE_API_KEY="", PAYMENT_FAKE_PROVIDER=True)
class OnlinePaymentTestCase(EndToEndTestCase):
    def order_url(self, period=PERIOD):
        return self.with_period(reverse("order", args=[self.hotel.pk]), period)

    def to_confirm_step(self, stay=STAY, period=PERIOD, **stay_data):
        url = self.order_url(period)
        self.client.get(url)
        self.post_step(url, "stay", {**stay, "room": self.room.pk, **stay_data})
        response = self.post_step(url, "guest", GUEST)
        self.assertContains(response, "Klopt alles?")
        return response

    def confirm(self, payment_method="online", **data):
        return self.post_step(
            self.order_url(),
            "confirm",
            {"terms": "on", "payment_method": payment_method, **data},
        )

    def pay(self, action="paid"):
        """Confirm online, then pay (or not) at the fake provider."""
        self.to_confirm_step()
        response = self.confirm()
        payment = Payment.objects.get(status=PaymentState.OPEN)
        self.assertEqual(response["Location"], SITE_URL + fake_checkout_path(payment))

        payment_page = path_of(response["Location"])
        response = self.client.get(payment_page)
        self.assertContains(
            response, "Testbetaling, er wordt geen echt geld afgeschreven"
        )
        self.assertContains(response, "€ 191,00")
        response = self.client.post(payment_page, {"action": action})
        return_url = response["Location"]
        self.assertEqual(
            return_url,
            SITE_URL + reverse("billing:payment_return", args=[payment.reference]),
        )
        with commit_hooks():
            return self.client.get(path_of(return_url))


class OnlinePaymentTests(OnlinePaymentTestCase):
    def test_pay_online_confirm_invoice_and_mail(self):
        # 1. Step 3 offers online payment; confirming holds the room.
        response = self.to_confirm_step()
        self.assertContains(response, "Nu online betalen (iDEAL)")

        response = self.pay()

        # 2. Back from the provider: "Mijn boeking", confirmed and paid.
        order = Order.objects.get()
        link = reverse("bookings:detail", kwargs={"token": order.token})
        self.assertRedirects(response, link, fetch_redirect_response=False)
        self.assertEqual(order.status, OrderStatus.CONFIRMED)
        self.assertEqual(order.payment_status, PaymentStatus.PAID)
        response = self.client.get(link)
        self.assertContains(response, "Je betaling is ontvangen.")
        self.assertContains(response, "Betaald via iDEAL op 1 maart 2027")
        self.assertContains(response, "Betaald")
        self.assertNotContains(response, "Je betaalt in het hotel")
        self.assert_private(response)

        # 3. The invoice, on the page and in the confirmation mail.
        invoice = order.invoices.get()
        self.assertEqual(invoice.total_incl, Decimal("191.00"))
        invoice_url = reverse("bookings:invoice", args=[order.token, invoice.pk])
        self.assertContains(response, f'href="{invoice_url}"')
        download = self.client.get(invoice_url)
        self.assertEqual(download.status_code, 200)
        self.assertTrue(download.content.startswith(b"%PDF"))

        [confirmation] = mail.outbox
        self.assertEqual(
            [filename for filename, _content, _mimetype in confirmation.attachments],
            [f"boeking_{order.pk}.pdf", invoice.filename],
        )
        self.assertEqual(confirmation.attachments[1][1], download.content)
        self.assertEqual(self.my_booking_path(confirmation), link)

        # 4. The room is taken.
        self.assertEqual(self.hotel_names(PERIOD), ["Hotel Centraal"])

    def test_payment_fails_and_the_room_is_free_again(self):
        self.to_confirm_step()
        self.confirm()
        # While the guest is paying, the room is held.
        self.assertEqual(self.hotel_names(PERIOD), ["Hotel Centraal"])
        order = Order.objects.get()
        self.assertEqual(order.status, OrderStatus.PENDING_PAYMENT)

        payment_page = path_of(Payment.objects.get().checkout_url)
        response = self.client.post(payment_page, {"action": "failed"})
        with commit_hooks():
            response = self.client.get(path_of(response["Location"]))

        # Back to the checkout with the dates of the booking, and a message.
        self.assertRedirects(
            response,
            self.with_period(reverse("order", args=[self.hotel.pk]), PERIOD),
            fetch_redirect_response=False,
        )
        response = self.client.get(response["Location"])
        self.assertContains(response, "De betaling is niet gelukt")
        self.assertContains(response, 'role="alert"')

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.EXPIRED)
        self.assertFalse(Invoice.objects.exists())
        self.assertEqual(mail.outbox, [])
        # The room is free again, also for this guest.
        self.assertEqual(
            self.hotel_names(PERIOD), ["Hotel Centraal", "Hotel de Gracht"]
        )
        response = self.client.get(self.order_url())
        self.assertContains(response, f'value="{self.room.pk}"')
        self.assertNotContains(response, "Geen kamers vrij in deze periode.")


class DiscountCodeEndToEndTests(OnlinePaymentTestCase):
    def test_valid_code_is_on_the_booking(self):
        DiscountCodeFactory(code="ZOMER", value=Decimal("10"))
        response = self.to_confirm_step(discount_code="zomer")
        self.assertContains(response, "Kortingscode ZOMER")
        self.assertContains(response, "− € 19,10")
        self.assertContains(response, "€ 171,90")

        with self.captureOnCommitCallbacks(execute=True):
            response = self.confirm(payment_method="at_hotel", expected_total="171.90")

        order = Order.objects.get()
        self.assertEqual(order.total_price, Decimal("171.90"))
        response = self.client.get(response["Location"])
        self.assertContains(response, "Je boeking staat vast")
        self.assertContains(response, "Subtotaal")
        self.assertContains(response, "€ 191,00")
        self.assertContains(response, "Kortingscode ZOMER")
        self.assertContains(response, "− € 19,10")
        self.assertContains(response, "€ 171,90")
        self.assertEqual(order.invoices.get().total_incl, Decimal("171.90"))

    def test_invalid_code(self):
        url = self.order_url()
        self.client.get(url)
        response = self.post_step(
            url, "stay", {**STAY, "room": self.room.pk, "discount_code": "GRATIS"}
        )
        self.assertContains(response, "Deze kortingscode is niet geldig.", count=1)
        self.assertEqual(response.context["wizard"]["steps"].current, "stay")
        self.assertFalse(Order.objects.exists())


class PriceChangedEndToEndTests(OnlinePaymentTestCase):
    def test_new_price_before_booking(self):
        response = self.to_confirm_step()
        self.assertContains(response, 'name="confirm-expected_total" value="191.00"')
        # Meanwhile staff sets a discount for stays of 2 nights or more.
        PricingSettings.load()
        PricingSettings.objects.filter(pk=1).update(
            long_stay_min_nights=2, long_stay_percent=Decimal("10")
        )

        response = self.confirm(payment_method="online", expected_total="191.00")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "De prijs van dit verblijf is gewijzigd.")
        self.assertContains(response, "Korting lang verblijf (10%)")
        self.assertContains(response, 'name="confirm-expected_total" value="171.90"')
        self.assertFalse(Order.objects.exists())
        self.assertFalse(Payment.objects.exists())

        response = self.confirm(payment_method="online", expected_total="171.90")
        payment = Payment.objects.get()
        self.assertEqual(response["Location"], payment.checkout_url)
        self.assertEqual(payment.amount, Decimal("171.90"))


class InvoiceOfAnotherBookingTests(OnlinePaymentTestCase):
    def test_token_only_opens_its_own_invoices(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.to_confirm_step()
            first = self.client.get(self.confirm(payment_method="at_hotel")["Location"])
        mine = Order.objects.get()
        with self.captureOnCommitCallbacks(execute=True):
            self.to_confirm_step(
                stay={"start_date": "2027-03-20", "end_date": "2027-03-21"},
                period={"van": "2027-03-20", "tot": "2027-03-21"},
            )
            self.confirm(payment_method="at_hotel")
        other = Order.objects.exclude(pk=mine.pk).get()
        self.assertEqual(first.status_code, 200)

        other_invoice = other.invoices.get()
        response = self.client.get(
            reverse("bookings:invoice", args=[mine.token, other_invoice.pk])
        )
        self.assertEqual(response.status_code, 404)
        response = self.client.get(
            reverse("bookings:invoice", args=[other.token, other_invoice.pk])
        )
        self.assertEqual(response.status_code, 200)


class PaidBookingCancelledTests(OnlinePaymentTestCase):
    def test_refund_and_credit_note(self):
        self.pay()
        order = Order.objects.get()
        link = reverse("bookings:detail", kwargs={"token": order.token})
        cancel_url = reverse("bookings:cancel", kwargs={"token": order.token})

        response = self.client.get(cancel_url)
        self.assertContains(response, "Je krijgt € 191,00 teruggestort.")
        with commit_hooks():
            response = self.client.post(cancel_url)
        self.assertRedirects(response, link, fetch_redirect_response=False)

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CANCELLED)
        self.assertEqual(order.payment_status, PaymentStatus.REFUNDED)
        refund = order.payments.get().refunds.get()
        self.assertEqual(refund.status, RefundState.REFUNDED)
        self.assertEqual(refund.amount, Decimal("191.00"))

        response = self.client.get(link)
        self.assertContains(response, "Je krijgt € 191,00 teruggestort")
        self.assertContains(response, "Teruggestort op 1 maart 2027: € 191,00")
        self.assertContains(response, "het betaalde bedrag is teruggestort")
        credit_note = order.invoices.get(kind=InvoiceKind.CREDIT_NOTE)
        credit_note_url = reverse(
            "bookings:invoice", args=[order.token, credit_note.pk]
        )
        self.assertContains(response, f'href="{credit_note_url}"')
        self.assertEqual(credit_note.total_incl, Decimal("-191.00"))
        download = self.client.get(credit_note_url)
        self.assertEqual(download.status_code, 200)
        self.assertEqual(
            download["Content-Disposition"],
            f'attachment; filename="{credit_note.filename}"',
        )

        # Confirmation, cancellation (with the credit note) and the refund.
        self.assertEqual(
            list(order.emails.values_list("kind", "status")),
            [
                (EmailKind.BOOKING_CONFIRMATION, EmailStatus.SENT),
                (EmailKind.BOOKING_CANCELLATION, EmailStatus.SENT),
                (EmailKind.PAYMENT_REFUND, EmailStatus.SENT),
            ],
        )
        cancellation = mail.outbox[1]
        self.assertEqual(
            [filename for filename, _content, _mimetype in cancellation.attachments],
            [credit_note.filename],
        )
