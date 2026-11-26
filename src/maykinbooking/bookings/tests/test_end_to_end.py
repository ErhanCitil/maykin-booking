"""
End-to-end flows through the test client: US1.2, US1.3, US2.1-US2.3, US3.1-US3.3.

A guest searches the city page with a period, opens the hotel, goes through the
three checkout steps, gets the confirmation mail with the PDF and the link, opens
"Mijn boeking" with that link, downloads the PDF and cancels. Afterwards the room
is free again on the city page and staff sees the cancelled booking with its log
in the dashboard.

Time is frozen in UTC; mails are sent with ``captureOnCommitCallbacks``.
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

from maykinbooking.accounts.tests.factories import StaffUserFactory
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
    "country": "NL",
}

MY_BOOKING_LINK = re.compile(re.escape(SITE_URL) + r"(/boeking/[0-9a-f-]{36}/)")


@freeze_time(NOW)
@override_settings(
    SITE_URL=SITE_URL,
    DEFAULT_FROM_EMAIL="boekingen@example.com",
    BOOKING_CONTACT_EMAIL="hulp@example.com",
)
class EndToEndTestCase(TestCase):
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
        cls.staff = StaffUserFactory(
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
        [(filename, content, mimetype)] = confirmation.attachments
        self.assertEqual(filename, f"boeking_{order.pk}.pdf")
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

        # 6. The PDF can be downloaded from there.
        pdf_url = reverse("bookings:pdf", kwargs={"token": order.token})
        self.assertContains(response, f'href="{pdf_url}"')
        response = self.client.get(pdf_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn(
            f'filename="boeking_{order.pk}.pdf"', response["Content-Disposition"]
        )
        self.assertTrue(response.content.startswith(b"%PDF"))
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
        self.assertEqual(cancellation.attachments, [])
        self.assertEqual(self.my_booking_path(cancellation), link)

        response = self.client.get(link)
        self.assertContains(response, "Je boeking is geannuleerd.")
        self.assertContains(response, "Deze boeking is geannuleerd.")
        self.assertNotContains(response, f'href="{cancel_url}"')

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
