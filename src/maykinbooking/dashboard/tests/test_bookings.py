"""
The bookings in the staff dashboard: list, CSV export, detail page, cancelling
and sending the confirmation again.

Testpunten 7 (staff part), 8 (sending again), 9 (dashboard part) and 12.
"""
import csv
import io
from datetime import timedelta
from decimal import Decimal
from smtplib import SMTPException
from unittest import mock

from django.contrib.messages import get_messages
from django.core import mail
from django.core.mail import EmailMultiAlternatives
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from maykinbooking.accounts.tests.factories import StaffUserFactory, UserFactory
from maykinbooking.base.models import (
    ActorType,
    EmailKind,
    EmailLog,
    EmailStatus,
    OrderStatus,
    OrderStatusChange,
)
from maykinbooking.base.tests.factories import (
    CityFactory,
    HotelFactory,
    OrderFactory,
    RoomFactory,
)
from maykinbooking.bookings.services.booking import (
    Actor,
    CancellationNotAllowed,
    InvalidTransition,
    cancel_booking,
    create_booking,
)

from ..views import csv_cell

GUEST = {
    "first_name": "Alex",
    "last_name": "de Landgraaf",
    "email": "alex@example.com",
    "address": "Keizersgracht 117",
    "zipcode": "1015CJ",
    "country": "NL",
}


@override_settings(SITE_URL="https://booking.example.com")
class BookingDashboardTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = StaffUserFactory(
            username="medewerker", first_name="Mila", last_name="de Vries"
        )
        cls.user = UserFactory(username="gast")
        cls.amsterdam = CityFactory(name="Amsterdam")
        cls.utrecht = CityFactory(name="Utrecht")
        cls.hotel = HotelFactory(
            city=cls.amsterdam, name="Hotel aan de Gracht", is_available=True
        )
        cls.other_hotel = HotelFactory(
            city=cls.utrecht, name="Hotel Domstad", is_available=True
        )
        cls.room = RoomFactory(
            hotel=cls.hotel, room_type="Double", price=Decimal("100.00")
        )
        cls.other_room = RoomFactory(
            hotel=cls.other_hotel, room_type="Single", price=Decimal("80.00")
        )

    def setUp(self):
        self.today = timezone.localdate()
        self.client.force_login(self.staff)

    def order(self, start_in=1, nights=2, room=None, **kwargs):
        """An order arriving ``start_in`` days from today."""
        room = room or self.room
        start = self.today + timedelta(days=start_in)
        return OrderFactory(
            hotel=room.hotel,
            room=room,
            start_date=start,
            end_date=start + timedelta(days=nights),
            **kwargs,
        )

    def mail_log(self, order, status, minutes_ago=0, **kwargs):
        return EmailLog.objects.create(
            order=order,
            kind=EmailKind.BOOKING_CONFIRMATION,
            recipient=order.email,
            status=status,
            created_at=timezone.now() - timedelta(minutes=minutes_ago),
            **kwargs,
        )

    def messages(self, response):
        return [str(message) for message in get_messages(response.wsgi_request)]


class AccessTests(BookingDashboardTestCase):
    """Testpunt 12: every booking page is for staff only."""

    def setUp(self):
        self.today = timezone.localdate()
        self.booking = self.order()

    def urls(self):
        pk = self.booking.pk
        return [
            ("get", reverse("dashboard:booking_list")),
            ("get", reverse("dashboard:booking_export")),
            ("get", reverse("dashboard:booking_detail", args=[pk])),
            ("get", reverse("dashboard:booking_cancel", args=[pk])),
            ("post", reverse("dashboard:booking_cancel", args=[pk])),
            ("post", reverse("dashboard:booking_resend", args=[pk])),
        ]

    def test_anonymous_users_are_sent_to_the_login_page(self):
        for method, url in self.urls():
            with self.subTest(method=method, url=url):
                response = getattr(self.client, method)(url)
                self.assertRedirects(
                    response,
                    f"{reverse('login')}?next={url}",
                    fetch_redirect_response=False,
                )

    def test_users_without_staff_status_get_403(self):
        self.client.force_login(self.user)
        for method, url in self.urls():
            with self.subTest(method=method, url=url):
                response = getattr(self.client, method)(
                    url, {"reason": "x", "confirm": "on"}
                )
                self.assertEqual(response.status_code, 403)

        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, OrderStatus.CONFIRMED)
        self.assertFalse(EmailLog.objects.exists())


class BookingListTests(BookingDashboardTestCase):
    """Testpunt 12: default sorting, filters, search and quick filters."""

    def get(self, **params):
        return self.client.get(reverse("dashboard:booking_list"), params)

    def pks(self, response):
        return [order.pk for order in response.context["orders"]]

    def test_default_shows_arrivals_from_today_oldest_first(self):
        later = self.order(start_in=10)
        self.order(start_in=-3)
        today = self.order(start_in=0)
        soon = self.order(start_in=2)

        response = self.get()

        self.assertEqual(self.pks(response), [today.pk, soon.pk, later.pk])
        self.assertTrue(response.context["shows_default_period"])
        self.assertContains(response, "aankomsten vanaf vandaag")

    def test_shows_the_columns(self):
        order = self.order(
            first_name="Jan",
            last_name="Jansen",
            email="jan@example.com",
            nights=3,
        )

        response = self.get()

        self.assertContains(response, f"#{order.pk}")
        self.assertContains(response, "Jan Jansen")
        self.assertContains(response, "jan@example.com")
        self.assertContains(response, "Hotel aan de Gracht")
        self.assertContains(response, "Amsterdam")
        self.assertContains(response, "Double")
        self.assertContains(response, "€ 300,00")
        self.assertContains(response, "Bevestigd")
        self.assertContains(response, "Betalen in het hotel")
        self.assertContains(
            response, reverse("dashboard:booking_detail", args=[order.pk])
        )

    def test_booking_numbers_have_no_thousand_separator(self):
        OrderFactory(
            id=12345,
            hotel=self.hotel,
            room=self.room,
            start_date=self.today,
            end_date=self.today + timedelta(days=1),
        )
        response = self.get()
        self.assertContains(response, "#12345")
        self.assertNotContains(response, "#12.345")

    def test_25_per_page_and_the_filters_stay_when_paginating(self):
        for _ in range(27):
            self.order()
        self.order(room=self.other_room)

        response = self.get(hotel=self.hotel.pk)

        self.assertEqual(len(response.context["orders"]), 25)
        self.assertEqual(response.context["paginator"].count, 27)
        self.assertContains(response, f"?hotel={self.hotel.pk}&amp;page=2")

        response = self.get(hotel=self.hotel.pk, page=2)
        self.assertEqual(len(response.context["orders"]), 2)
        self.assertContains(response, f"?hotel={self.hotel.pk}&amp;page=1")

    def test_filter_on_city_and_hotel(self):
        amsterdam = self.order()
        utrecht = self.order(room=self.other_room)

        self.assertEqual(self.pks(self.get(stad=self.utrecht.pk)), [utrecht.pk])
        self.assertEqual(self.pks(self.get(hotel=self.hotel.pk)), [amsterdam.pk])

    def test_filter_on_status(self):
        self.order()
        cancelled = self.order(status=OrderStatus.CANCELLED)

        response = self.get(status=OrderStatus.CANCELLED)

        self.assertEqual(self.pks(response), [cancelled.pk])

    def test_arrival_filter_replaces_the_default_period(self):
        long_ago = self.order(start_in=-20)
        last_week = self.order(start_in=-7)
        yesterday = self.order(start_in=-1)
        self.order(start_in=3)

        from_date = (self.today - timedelta(days=8)).isoformat()
        to_date = (self.today - timedelta(days=1)).isoformat()
        response = self.get(aankomst_van=from_date, aankomst_tot=to_date)

        self.assertEqual(self.pks(response), [last_week.pk, yesterday.pk])
        self.assertFalse(response.context["shows_default_period"])

        # Only an end date: everything that arrived until then.
        response = self.get(aankomst_tot=to_date)
        self.assertEqual(self.pks(response), [long_ago.pk, last_week.pk, yesterday.pk])

    def test_invalid_date_shows_an_error_and_keeps_the_default(self):
        self.order(start_in=-2)
        upcoming = self.order(start_in=2)

        response = self.get(aankomst_van="morgen")

        self.assertEqual(response.status_code, 200)
        self.assertIn("aankomst_van", response.context["form"].errors)
        self.assertEqual(self.pks(response), [upcoming.pk])

    def test_search_on_name_email_and_number_also_in_the_past(self):
        jan = self.order(
            start_in=-30, first_name="Jan", last_name="Jansen", email="jj@example.com"
        )
        other = self.order(first_name="Piet", last_name="Pietersen")

        for query in ("jan", "JANSEN", "Jan Jansen", "jj@example", str(jan.pk)):
            with self.subTest(query=query):
                self.assertEqual(self.pks(self.get(q=query)), [jan.pk])
        self.assertEqual(self.pks(self.get(q=f" #{other.pk} ")), [other.pk])
        self.assertEqual(self.pks(self.get(q="Jan Pietersen")), [])
        self.assertEqual(self.pks(self.get(q="99999999999999999999")), [])
        self.assertEqual(self.pks(self.get(q="²")), [])

    def test_a_number_only_finds_the_booking_with_that_number(self):
        booking = self.order()
        # An e-mail address with the same digits does not match.
        self.order(email=f"gast{booking.pk}@example.com")

        self.assertEqual(self.pks(self.get(q=str(booking.pk))), [booking.pk])

    def test_search_combines_with_the_other_filters(self):
        self.order(first_name="Jan")
        cancelled = self.order(first_name="Jan", status=OrderStatus.CANCELLED)

        response = self.get(q="jan", status=OrderStatus.CANCELLED)

        self.assertEqual(self.pks(response), [cancelled.pk])

    def test_quick_filter_arrivals_today(self):
        today = self.order(start_in=0)
        self.order(start_in=1)
        self.order(start_in=-1)

        response = self.get(snel="aankomst_vandaag")

        self.assertEqual(self.pks(response), [today.pk])

    def test_quick_filter_departures_today(self):
        # Arrived in the past, so outside the default period.
        leaving = self.order(start_in=-2, nights=2)
        self.order(start_in=-2, nights=3)
        self.order(start_in=0)

        response = self.get(snel="vertrek_vandaag")

        self.assertEqual(self.pks(response), [leaving.pk])

    def test_quick_filter_failed_mail_and_badge(self):
        failed = self.order()
        self.mail_log(failed, EmailStatus.SENT, minutes_ago=10)
        self.mail_log(failed, EmailStatus.FAILED)
        fixed = self.order()
        self.mail_log(fixed, EmailStatus.FAILED, minutes_ago=10)
        self.mail_log(fixed, EmailStatus.SENT)
        self.order()

        response = self.get()
        self.assertContains(response, "mail mislukt", count=1)

        response = self.get(mail="mislukt")
        self.assertEqual(self.pks(response), [failed.pk])

    def test_quick_filters_keep_the_other_filters_and_toggle(self):
        response = self.get(hotel=self.hotel.pk, snel="aankomst_vandaag")

        quick = {item["label"]: item for item in response.context["quick_filters"]}
        arrivals = quick["Aankomsten vandaag"]
        self.assertTrue(arrivals["active"])
        # Clicking the active filter turns it off, the hotel stays.
        self.assertEqual(arrivals["query"], f"hotel={self.hotel.pk}")
        self.assertEqual(
            quick["Vertrekken vandaag"]["query"],
            f"hotel={self.hotel.pk}&snel=vertrek_vandaag",
        )
        self.assertEqual(
            quick["Mail mislukt"]["query"],
            f"hotel={self.hotel.pk}&snel=aankomst_vandaag&mail=mislukt",
        )
        # The form submits the active quick filter again.
        self.assertContains(
            response, '<input type="hidden" name="snel" value="aankomst_vandaag"'
        )

    def test_export_link_has_the_filters(self):
        response = self.get(hotel=self.hotel.pk, q="jan")
        export_url = reverse("dashboard:booking_export")
        self.assertContains(response, f"{export_url}?hotel={self.hotel.pk}&amp;q=jan")

    def test_number_of_queries_does_not_grow_with_the_orders(self):
        def count_queries():
            with CaptureQueriesContext(connection) as context:
                response = self.get()
                self.assertEqual(response.status_code, 200)
            return len(context.captured_queries), len(response.context["orders"])

        for _ in range(2):
            self.mail_log(self.order(), EmailStatus.FAILED)
        few = count_queries()
        for _ in range(8):
            self.mail_log(self.order(room=self.other_room), EmailStatus.SENT)
        many = count_queries()

        self.assertEqual((few[1], many[1]), (2, 10))
        self.assertEqual(few[0], many[0])


class BookingExportTests(BookingDashboardTestCase):
    """Testpunt 12: the CSV follows the filters and neutralises formulas."""

    def export(self, **params):
        response = self.client.get(reverse("dashboard:booking_export"), params)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.streaming)
        content = b"".join(response.streaming_content).decode("utf-8")
        return response, content

    def rows(self, content):
        return list(csv.reader(io.StringIO(content.lstrip("\ufeff")), delimiter=";"))

    def test_csv_for_excel(self):
        order = self.order(
            first_name="Zoë",
            last_name="Ñúñez",
            email="zoe@example.com",
            nights=3,
        )

        response, content = self.export()

        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertEqual(
            response["Content-Disposition"],
            f'attachment; filename="boekingen-{self.today:%Y-%m-%d}.csv"',
        )
        self.assertTrue(content.startswith("\ufeffNummer;Voornaam;Achternaam;"))
        header, row = self.rows(content)
        self.assertEqual(len(header), 14)
        record = dict(zip(header, row))
        self.assertEqual(record["Nummer"], str(order.pk))
        self.assertEqual(record["Voornaam"], "Zoë")
        self.assertEqual(record["Achternaam"], "Ñúñez")
        self.assertEqual(record["E-mail"], "zoe@example.com")
        self.assertEqual(record["Hotel"], "Hotel aan de Gracht")
        self.assertEqual(record["Stad"], "Amsterdam")
        self.assertEqual(record["Kamer"], "Double")
        self.assertEqual(record["Aankomst"], order.start_date.isoformat())
        self.assertEqual(record["Vertrek"], order.end_date.isoformat())
        self.assertEqual(record["Nachten"], "3")
        self.assertEqual(record["Totaal"], "300,00")
        self.assertEqual(record["Status"], "Bevestigd")
        self.assertEqual(record["Betaalstatus"], "Betalen in het hotel")

    def test_follows_the_filters_of_the_list_without_pagination(self):
        for _ in range(30):
            self.order()
        self.order(room=self.other_room)
        self.order(start_in=-5)

        _, content = self.export(hotel=self.hotel.pk)

        rows = self.rows(content)[1:]
        self.assertEqual(len(rows), 30)
        self.assertEqual({row[4] for row in rows}, {"Hotel aan de Gracht"})

    def test_quick_filters_and_search_apply(self):
        self.order(start_in=0, first_name="Vandaag")
        self.order(start_in=-1, first_name="Gisteren")
        failed = self.order(start_in=0, first_name="Mislukt")
        self.mail_log(failed, EmailStatus.FAILED)

        _, content = self.export(snel="aankomst_vandaag", mail="mislukt")
        self.assertEqual([row[1] for row in self.rows(content)[1:]], ["Mislukt"])

        _, content = self.export(q="gisteren")
        self.assertEqual([row[1] for row in self.rows(content)[1:]], ["Gisteren"])

    def test_formulas_are_neutralised(self):
        self.order(
            first_name="=cmd|' /C calc'!A0",
            last_name="+31 6 1234",
            email="@example.com",
        )
        hotel = HotelFactory(
            city=self.amsterdam, name="-2+3", is_available=True, hotel_id="x"
        )
        self.order(room=RoomFactory(hotel=hotel))

        _, content = self.export()

        rows = self.rows(content)
        record = dict(zip(rows[0], rows[1]))
        self.assertEqual(record["Voornaam"], "'=cmd|' /C calc'!A0")
        self.assertEqual(record["Achternaam"], "'+31 6 1234")
        self.assertEqual(record["E-mail"], "'@example.com")
        self.assertEqual(dict(zip(rows[0], rows[2]))["Hotel"], "'-2+3")
        self.assertNotIn(";=cmd", content)

    def test_csv_cell(self):
        for value in ("=1+1", "+1", "-1", "@SUM(A1)", "\tx", "\rx"):
            with self.subTest(value=value):
                self.assertEqual(csv_cell(value), f"'{value}")
        self.assertEqual(csv_cell("Jan"), "Jan")
        self.assertEqual(csv_cell("a=b"), "a=b")
        self.assertEqual(csv_cell(12), "12")
        self.assertEqual(csv_cell(None), "")


class BookingDetailTests(BookingDashboardTestCase):
    def book(self, start_in=7, nights=2):
        start = self.today + timedelta(days=start_in)
        return create_booking(
            room_id=self.room.pk,
            start=start,
            end=start + timedelta(days=nights),
            guest=GUEST,
            language="nl",
            actor=Actor.guest(),
        )

    def detail(self, order):
        return self.client.get(reverse("dashboard:booking_detail", args=[order.pk]))

    def test_shows_guest_stay_and_status(self):
        order = self.book()

        response = self.detail(order)

        self.assertContains(response, f"Boeking #{order.pk}")
        self.assertContains(response, "Alex de Landgraaf")
        self.assertContains(response, 'href="mailto:alex@example.com"')
        self.assertContains(response, "Keizersgracht 117")
        self.assertContains(response, "1015CJ")
        self.assertContains(response, "Hotel aan de Gracht")
        self.assertContains(response, "Double")
        self.assertContains(response, "Bevestigd")
        self.assertContains(response, "Betalen in het hotel")
        self.assertContains(response, "Totaal voor 2 nachten")

    def test_shows_the_snapshot_prices_not_the_current_room_price(self):
        # Testpunt 9: a new room price does not change existing orders.
        order = self.book(nights=3)
        self.room.price = Decimal("250.00")
        self.room.save()

        response = self.detail(order)

        self.assertContains(response, "€ 100,00", count=3)
        self.assertContains(response, "€ 300,00")
        self.assertNotContains(response, "250,00")

    def test_shows_the_log(self):
        order = self.book()
        cancel_booking(
            order.pk,
            actor=Actor.staff(self.staff),
            reason="Gast belde: <ziek>",
            enforce_deadline=False,
        )

        response = self.detail(order)

        log = response.context["status_changes"]
        self.assertEqual(
            [(row["old"], row["new"]) for row in log],
            [
                ("", "Bevestigd"),
                ("", "Betalen in het hotel"),
                ("Bevestigd", "Geannuleerd"),
            ],
        )
        self.assertContains(response, "Mila de Vries")
        self.assertContains(response, "Gast belde: &lt;ziek&gt;")
        self.assertContains(response, "Betaalstatus")

    def test_shows_the_mails(self):
        order = self.book()
        self.mail_log(order, EmailStatus.SENT, minutes_ago=5)
        self.mail_log(
            order,
            EmailStatus.FAILED,
            error="SMTPException: Connection refused",
            triggered_by=self.staff,
        )

        response = self.detail(order)

        self.assertContains(response, "Boekingsbevestiging", count=2)
        self.assertContains(response, "Verstuurd")
        self.assertContains(response, "Mislukt")
        self.assertContains(response, "SMTPException: Connection refused")
        self.assertContains(response, "door medewerker")

    def test_links_to_my_booking_of_the_guest(self):
        order = self.book()

        response = self.detail(order)

        self.assertContains(
            response, f'href="https://booking.example.com/boeking/{order.token}/"'
        )

    def test_actions_for_a_confirmed_booking(self):
        order = self.book()

        response = self.detail(order)

        self.assertContains(
            response, reverse("dashboard:booking_cancel", args=[order.pk])
        )
        self.assertContains(
            response,
            f'action="{reverse("dashboard:booking_resend", args=[order.pk])}"',
        )
        self.assertContains(response, "Bevestigingsmail opnieuw versturen")
        self.assertNotContains(response, "Annuleringsmail opnieuw versturen")

    def test_a_cancelled_booking_can_only_get_the_cancellation_again(self):
        order = self.order(status=OrderStatus.CANCELLED)

        response = self.detail(order)

        self.assertNotContains(
            response, reverse("dashboard:booking_cancel", args=[order.pk])
        )
        self.assertContains(
            response,
            f'action="{reverse("dashboard:booking_resend", args=[order.pk])}"',
        )
        self.assertContains(response, "Annuleringsmail opnieuw versturen")
        self.assertNotContains(response, "Bevestigingsmail opnieuw versturen")

    def test_no_actions_for_pending_or_expired_bookings(self):
        for status in (OrderStatus.PENDING_PAYMENT, OrderStatus.EXPIRED):
            with self.subTest(status=status):
                order = self.order(status=status)

                response = self.detail(order)

                self.assertNotContains(
                    response, reverse("dashboard:booking_cancel", args=[order.pk])
                )
                self.assertNotContains(
                    response, reverse("dashboard:booking_resend", args=[order.pk])
                )

    def test_unknown_booking_is_404(self):
        response = self.client.get(reverse("dashboard:booking_detail", args=[999999]))
        self.assertEqual(response.status_code, 404)

    def test_number_of_queries_does_not_grow_with_the_log_and_mails(self):
        def count_queries(order):
            with CaptureQueriesContext(connection) as context:
                self.assertEqual(self.detail(order).status_code, 200)
            return len(context.captured_queries)

        few = self.order(nights=1)
        self.mail_log(few, EmailStatus.SENT, triggered_by=self.staff)
        many = self.order(nights=10)
        for _ in range(5):
            self.mail_log(many, EmailStatus.SENT, triggered_by=self.staff)

        self.assertEqual(count_queries(few), count_queries(many))


class BookingCancelTests(BookingDashboardTestCase):
    """Testpunt 7, staff part: always possible, but only with a reason."""

    def cancel_url(self, order):
        return reverse("dashboard:booking_cancel", args=[order.pk])

    def detail_url(self, order):
        return reverse("dashboard:booking_detail", args=[order.pk])

    def test_shows_the_form(self):
        order = self.order()

        response = self.client.get(self.cancel_url(order))

        self.assertContains(response, "Boeking annuleren?")
        self.assertContains(response, 'name="reason"')
        self.assertContains(response, 'name="confirm"')

    def test_staff_can_cancel_after_the_guest_deadline_with_a_reason(self):
        # Arrival today: the guest can no longer cancel.
        order = self.order(start_in=0)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                self.cancel_url(order),
                {"reason": "Gast belde, is ziek", "confirm": "on"},
            )

        self.assertRedirects(response, self.detail_url(order))
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CANCELLED)
        change = order.status_changes.get()
        self.assertEqual(change.actor_type, ActorType.STAFF)
        self.assertEqual(change.actor_user, self.staff)
        self.assertEqual(change.actor_label, "Mila de Vries")
        self.assertEqual(change.reason, "Gast belde, is ziek")
        self.assertEqual(
            self.messages(response),
            [
                f"Boeking #{order.pk} is geannuleerd. De gast krijgt een annuleringsmail."
            ],
        )
        # The cancellation mail is sent after the commit.
        self.assertEqual(len(mail.outbox), 1)
        log = EmailLog.objects.get()
        self.assertEqual(log.kind, EmailKind.BOOKING_CANCELLATION)
        self.assertEqual(log.status, EmailStatus.SENT)
        self.assertEqual(log.triggered_by, self.staff)

    def test_reason_is_required(self):
        order = self.order()

        for reason in ("", "   "):
            with self.subTest(reason=reason):
                response = self.client.post(
                    self.cancel_url(order), {"reason": reason, "confirm": "on"}
                )
                self.assertEqual(response.status_code, 200)
                self.assertIn("reason", response.context["form"].errors)

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CONFIRMED)
        self.assertFalse(OrderStatusChange.objects.exists())

    def test_confirmation_is_required(self):
        order = self.order()

        response = self.client.post(self.cancel_url(order), {"reason": "Dubbel"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("confirm", response.context["form"].errors)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CONFIRMED)

    def test_cancelling_twice_gives_one_log_and_one_mail(self):
        order = self.order()
        data = {"reason": "Dubbele boeking", "confirm": "on"}

        with self.captureOnCommitCallbacks(execute=True):
            # Following the redirect shows (and so clears) the first message.
            self.client.post(self.cancel_url(order), data, follow=True)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self.cancel_url(order), data)

        self.assertRedirects(response, self.detail_url(order))
        self.assertEqual(self.messages(response), ["Deze boeking is al geannuleerd."])
        self.assertEqual(order.status_changes.count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_already_cancelled_booking_gives_a_message(self):
        order = self.order(status=OrderStatus.CANCELLED)

        response = self.client.get(self.cancel_url(order))

        self.assertRedirects(response, self.detail_url(order))
        self.assertEqual(self.messages(response), ["Deze boeking is al geannuleerd."])

    def test_expired_booking_cannot_be_cancelled(self):
        order = self.order(status=OrderStatus.EXPIRED)

        for method in ("get", "post"):
            with self.subTest(method=method):
                response = getattr(self.client, method)(
                    self.cancel_url(order), {"reason": "x", "confirm": "on"}
                )
                self.assertRedirects(response, self.detail_url(order))
                self.assertEqual(
                    self.messages(response),
                    [
                        "Een boeking met de status “Verlopen” kan niet geannuleerd worden."
                    ],
                )
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.EXPIRED)

    def test_status_change_while_the_form_was_open_gives_a_message(self):
        order = self.order()

        with mock.patch(
            "maykinbooking.dashboard.views.cancel_booking",
            side_effect=InvalidTransition("expired meanwhile"),
        ):
            response = self.client.post(
                self.cancel_url(order), {"reason": "x", "confirm": "on"}
            )

        self.assertRedirects(response, self.detail_url(order))
        self.assertEqual(len(self.messages(response)), 1)

    def test_unknown_booking_is_404(self):
        response = self.client.get(reverse("dashboard:booking_cancel", args=[999999]))
        self.assertEqual(response.status_code, 404)


class BookingResendTests(BookingDashboardTestCase):
    """
    Testpunt 8: sending a mail again gives a new EmailLog. Which mail follows
    from the status: the confirmation says the booking stands.
    """

    def resend(self, order):
        return self.client.post(reverse("dashboard:booking_resend", args=[order.pk]))

    def test_sends_the_confirmation_of_a_confirmed_booking_again(self):
        order = self.order(email="gast@example.com")
        first = self.mail_log(order, EmailStatus.FAILED, minutes_ago=5)

        response = self.resend(order)

        self.assertRedirects(
            response, reverse("dashboard:booking_detail", args=[order.pk])
        )
        log = EmailLog.objects.exclude(pk=first.pk).get()
        self.assertEqual(log.kind, EmailKind.BOOKING_CONFIRMATION)
        self.assertEqual(log.status, EmailStatus.SENT)
        self.assertEqual(log.triggered_by, self.staff)
        self.assertEqual(log.recipient, "gast@example.com")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["gast@example.com"])
        ((filename, _, _),) = mail.outbox[0].attachments
        self.assertEqual(filename, f"boeking_{order.pk}.pdf")
        self.assertEqual(
            self.messages(response),
            ["De bevestigingsmail is opnieuw verstuurd naar gast@example.com."],
        )

        # The latest mail went out, so the list no longer warns.
        response = self.client.get(reverse("dashboard:booking_list"))
        self.assertNotContains(response, "mail mislukt")

    def test_sends_the_cancellation_of_a_cancelled_booking_again(self):
        order = self.order(email="gast@example.com", status=OrderStatus.CANCELLED)

        response = self.resend(order)

        self.assertRedirects(
            response, reverse("dashboard:booking_detail", args=[order.pk])
        )
        log = EmailLog.objects.get()
        self.assertEqual(log.kind, EmailKind.BOOKING_CANCELLATION)
        self.assertEqual(log.status, EmailStatus.SENT)
        self.assertEqual(log.triggered_by, self.staff)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("geannuleerd", mail.outbox[0].subject)
        self.assertEqual(
            self.messages(response),
            ["De annuleringsmail is opnieuw verstuurd naar gast@example.com."],
        )

    def test_failure_is_logged_and_shown(self):
        for status, kind, name in (
            (OrderStatus.CONFIRMED, EmailKind.BOOKING_CONFIRMATION, "bevestigingsmail"),
            (OrderStatus.CANCELLED, EmailKind.BOOKING_CANCELLATION, "annuleringsmail"),
        ):
            with self.subTest(status=status):
                order = self.order(status=status)

                with mock.patch.object(
                    EmailMultiAlternatives,
                    "send",
                    side_effect=SMTPException("Connection refused"),
                ):
                    response = self.client.post(
                        reverse("dashboard:booking_resend", args=[order.pk]),
                        follow=True,
                    )

                self.assertRedirects(
                    response, reverse("dashboard:booking_detail", args=[order.pk])
                )
                log = order.emails.get()
                self.assertEqual(log.kind, kind)
                self.assertEqual(log.status, EmailStatus.FAILED)
                self.assertIn("Connection refused", log.error)
                self.assertEqual(log.triggered_by, self.staff)
                self.assertEqual(
                    [str(m) for m in response.context["messages"]],
                    [
                        f"Het versturen van de {name} is mislukt. "
                        "De foutmelding staat bij de verstuurde mails."
                    ],
                )
                order.refresh_from_db()
                self.assertEqual(order.status, status)

        response = self.client.get(
            reverse("dashboard:booking_list"), {"mail": "mislukt"}
        )
        self.assertEqual(len(response.context["orders"]), 2)

    def test_pending_and_expired_bookings_get_a_message_and_no_mail(self):
        for status, label in (
            (OrderStatus.PENDING_PAYMENT, "Wacht op betaling"),
            (OrderStatus.EXPIRED, "Verlopen"),
        ):
            with self.subTest(status=status):
                order = self.order(status=status)

                response = self.client.post(
                    reverse("dashboard:booking_resend", args=[order.pk]),
                    follow=True,
                )

                self.assertRedirects(
                    response, reverse("dashboard:booking_detail", args=[order.pk])
                )
                self.assertEqual(
                    [str(m) for m in response.context["messages"]],
                    [
                        f"Bij een boeking met de status “{label}” kan geen mail "
                        "opnieuw verstuurd worden."
                    ],
                )
        self.assertFalse(EmailLog.objects.exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_get_is_not_allowed(self):
        order = self.order()

        response = self.client.get(reverse("dashboard:booking_resend", args=[order.pk]))

        self.assertEqual(response.status_code, 405)
        self.assertFalse(EmailLog.objects.exists())

    def test_unknown_booking_is_404(self):
        response = self.client.post(reverse("dashboard:booking_resend", args=[999999]))
        self.assertEqual(response.status_code, 404)


class StayOverTests(BookingDashboardTestCase):
    """
    A confirmed booking whose stay is over (from the day after the departure day
    on) is labelled "Verblijf voorbij" and can no longer be cancelled, also not by
    staff. On the departure day itself staff can still cancel (a no-show).
    """

    over_message = (
        "Het verblijf is voorbij; deze boeking kan niet meer geannuleerd worden."
    )

    def test_list_labels_the_stays_that_are_over(self):
        self.order(start_in=-4, nights=2)  # left two days ago
        self.order(start_in=-3, nights=2)  # left yesterday
        self.order(start_in=-2, nights=2)  # leaves today: not over yet
        self.order(start_in=-1, nights=2)  # still staying
        self.order(start_in=-4, nights=2, status=OrderStatus.CANCELLED)

        response = self.client.get(
            reverse("dashboard:booking_list"),
            {"aankomst_tot": self.today.isoformat()},
        )

        # Only the two confirmed stays that ended before today.
        self.assertEqual(response.context["paginator"].count, 5)
        self.assertContains(response, "Verblijf voorbij", count=2)

    def test_detail_has_the_label_and_no_cancel_button(self):
        order = self.order(start_in=-3, nights=2)

        response = self.client.get(reverse("dashboard:booking_detail", args=[order.pk]))

        self.assertContains(response, "Verblijf voorbij")
        self.assertContains(response, "Bevestigd · verblijf voorbij")
        self.assertFalse(response.context["can_cancel"])
        self.assertNotContains(
            response, reverse("dashboard:booking_cancel", args=[order.pk])
        )

    def test_the_server_refuses_to_cancel(self):
        for start_in in (-4, -3):  # left two days ago, left yesterday
            order = self.order(start_in=start_in, nights=2)
            url = reverse("dashboard:booking_cancel", args=[order.pk])
            for method in ("get", "post"):
                with self.subTest(start_in=start_in, method=method):
                    response = getattr(self.client, method)(
                        url, {"reason": "Te laat", "confirm": "on"}
                    )
                    self.assertRedirects(
                        response,
                        reverse("dashboard:booking_detail", args=[order.pk]),
                    )
                    self.assertEqual(self.messages(response), [self.over_message])
            order.refresh_from_db()
            self.assertEqual(order.status, OrderStatus.CONFIRMED)
        self.assertFalse(OrderStatusChange.objects.exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_staff_can_cancel_on_the_day_of_departure(self):
        # A no-show for a single night: the stay ends today.
        order = self.order(start_in=-1, nights=1)
        detail = self.client.get(reverse("dashboard:booking_detail", args=[order.pk]))
        self.assertNotContains(detail, "Verblijf voorbij")
        self.assertTrue(detail.context["can_cancel"])

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("dashboard:booking_cancel", args=[order.pk]),
                {"reason": "No-show", "confirm": "on"},
            )

        self.assertRedirects(
            response, reverse("dashboard:booking_detail", args=[order.pk])
        )
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CANCELLED)
        self.assertEqual(len(mail.outbox), 1)

    def test_refusal_of_the_service_gives_a_message(self):
        # The check of the page passed, but the service refuses (e.g. the stay
        # ended while the form was open).
        order = self.order()

        with mock.patch(
            "maykinbooking.dashboard.views.cancel_booking",
            side_effect=CancellationNotAllowed("Het verblijf is voorbij."),
        ):
            response = self.client.post(
                reverse("dashboard:booking_cancel", args=[order.pk]),
                {"reason": "x", "confirm": "on"},
            )

        self.assertRedirects(
            response, reverse("dashboard:booking_detail", args=[order.pk])
        )
        self.assertEqual(
            self.messages(response), ["Deze boeking kan niet geannuleerd worden."]
        )
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CONFIRMED)


class ResendCooldownTests(BookingDashboardTestCase):
    """
    A mail of the same kind is not sent again within two minutes, unless the
    previous one failed.
    """

    cooldown_message = (
        "Er is net al een mail verstuurd; probeer het over een paar minuten opnieuw."
    )

    def resend(self, order):
        return self.client.post(reverse("dashboard:booking_resend", args=[order.pk]))

    def test_same_kind_within_two_minutes_is_refused(self):
        for status in (EmailStatus.SENT, EmailStatus.PENDING):
            with self.subTest(status=status):
                order = self.order()
                self.mail_log(order, status, minutes_ago=1)

                response = self.resend(order)

                self.assertRedirects(
                    response, reverse("dashboard:booking_detail", args=[order.pk])
                )
                self.assertEqual(self.messages(response), [self.cooldown_message])
                self.assertEqual(order.emails.count(), 1)
        self.assertEqual(len(mail.outbox), 0)

    def test_a_double_click_sends_one_mail(self):
        order = self.order()

        self.client.post(
            reverse("dashboard:booking_resend", args=[order.pk]), follow=True
        )
        response = self.resend(order)

        self.assertEqual(self.messages(response), [self.cooldown_message])
        self.assertEqual(order.emails.count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_a_failed_mail_can_be_sent_again_right_away(self):
        order = self.order()
        self.mail_log(order, EmailStatus.FAILED, minutes_ago=0, error="SMTP weg")

        response = self.resend(order)

        self.assertRedirects(
            response, reverse("dashboard:booking_detail", args=[order.pk])
        )
        self.assertNotIn(self.cooldown_message, self.messages(response))
        self.assertEqual(
            list(order.emails.order_by("pk").values_list("status", flat=True)),
            [EmailStatus.FAILED, EmailStatus.SENT],
        )
        self.assertEqual(len(mail.outbox), 1)

        # The new mail did not fail, so now the cooldown applies again.
        self.assertEqual(self.messages(self.resend(order)), [self.cooldown_message])
        self.assertEqual(len(mail.outbox), 1)

    def test_after_two_minutes_the_mail_is_sent_again(self):
        order = self.order()
        self.mail_log(order, EmailStatus.SENT, minutes_ago=3)

        self.resend(order)

        self.assertEqual(order.emails.count(), 2)
        self.assertEqual(len(mail.outbox), 1)

    def test_a_recent_mail_of_another_kind_does_not_count(self):
        # Just confirmed, then cancelled: the cancellation can be sent again.
        order = self.order(status=OrderStatus.CANCELLED)
        self.mail_log(order, EmailStatus.SENT)

        self.resend(order)

        self.assertEqual(
            order.emails.filter(kind=EmailKind.BOOKING_CANCELLATION).count(), 1
        )
        self.assertEqual(len(mail.outbox), 1)


class NeverCacheTests(BookingDashboardTestCase):
    """The dashboard shows guest data: no page may be kept in a cache."""

    def test_all_pages_are_never_cached(self):
        order = self.order()
        for url in (
            reverse("dashboard:home"),
            reverse("dashboard:hotel_edit", args=[self.hotel.pk]),
            reverse("dashboard:room_edit", args=[self.room.pk]),
            reverse("dashboard:room_blocks", args=[self.room.pk]),
            reverse("dashboard:booking_list"),
            reverse("dashboard:booking_export"),
            reverse("dashboard:booking_detail", args=[order.pk]),
            reverse("dashboard:booking_cancel", args=[order.pk]),
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                cache_control = response["Cache-Control"]
                for directive in ("no-cache", "no-store", "must-revalidate", "private"):
                    self.assertIn(directive, cache_control)
                self.assertTrue(response.has_header("Expires"))

    def test_redirects_after_a_post_are_never_cached_either(self):
        order = self.order()
        response = self.client.post(
            reverse("dashboard:booking_resend", args=[order.pk])
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("no-store", response["Cache-Control"])


class ExportLogTests(BookingDashboardTestCase):
    """Every export is logged: who, how many rows and which filters, no guest data."""

    def test_export_is_logged_without_personal_data(self):
        self.order(first_name="Jan", last_name="Jansen", email="jan@example.com")
        self.order(room=self.other_room, first_name="Jan")

        with self.assertLogs("maykinbooking.dashboard.views", "INFO") as logs:
            response = self.client.get(
                reverse("dashboard:booking_export"),
                {"q": "jan@example.com", "hotel": self.hotel.pk, "status": "confirmed"},
            )
            b"".join(response.streaming_content)

        self.assertEqual(
            logs.output,
            [
                "INFO:maykinbooking.dashboard.views:booking export "
                f"user={self.staff.pk} rows=1 "
                f"filters=hotel={self.hotel.pk},q=*,status=confirmed"
            ],
        )

    def test_export_without_filters(self):
        self.order()

        with self.assertLogs("maykinbooking.dashboard.views", "INFO") as logs:
            response = self.client.get(reverse("dashboard:booking_export"))
            b"".join(response.streaming_content)

        self.assertIn("rows=1 filters=-", logs.output[0])
