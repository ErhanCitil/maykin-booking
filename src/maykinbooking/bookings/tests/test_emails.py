"""Testpunt 8: the booking mails, their log and their PDF attachment."""
from datetime import timedelta
from decimal import Decimal
from smtplib import SMTPException
from unittest import mock

from django.core import mail
from django.core.mail import EmailMultiAlternatives
from django.db import connection, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from maykinbooking.accounts.tests.factories import StaffUserFactory
from maykinbooking.base.models import EmailLog, Order, OrderStatus
from maykinbooking.base.tests.factories import HotelFactory, RoomFactory

from ..emails import (
    MAX_ERROR_LENGTH,
    booking_context,
    last_free_cancellation_day,
    send_booking_email,
)
from ..pdf import render_order_pdf
from ..services.booking import (
    Actor,
    cancel_booking,
    create_booking,
    guest_cancellation_deadline,
)

GUEST = {
    "first_name": "Alex",
    "last_name": "de Landgraaf",
    "email": "alex@example.com",
    "address": "Keizersgracht 117",
    "zipcode": "1015CJ",
    "country": "NL",
}


@override_settings(
    SITE_URL="https://booking.example.com",
    DEFAULT_FROM_EMAIL="boekingen@example.com",
    BOOKING_CONTACT_EMAIL="hulp@example.com",
)
class BookingEmailTestCase(TestCase):
    def setUp(self):
        hotel = HotelFactory(is_available=True, name="Hotel & Spa <Amsterdam>")
        self.room = RoomFactory(hotel=hotel, price=Decimal("100.00"), inventory=1)
        self.start = timezone.localdate() + timedelta(days=7)

    def book(self, nights=2):
        return create_booking(
            room_id=self.room.pk,
            start=self.start,
            end=self.start + timedelta(days=nights),
            guest=GUEST,
            language="nl",
            actor=Actor.guest(),
        )

    def book_and_commit(self, nights=2):
        with self.captureOnCommitCallbacks(execute=True):
            return self.book(nights)


class ConfirmationEmailTests(BookingEmailTestCase):
    def test_one_mail_with_pdf_and_link_after_commit(self):
        order = self.book_and_commit()

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["alex@example.com"])
        self.assertEqual(message.from_email, "boekingen@example.com")
        self.assertEqual(message.reply_to, ["hulp@example.com"])
        link = f"https://booking.example.com/boeking/{order.token}/"
        self.assertIn(link, message.body)
        html, mimetype = message.alternatives[0]
        self.assertEqual(mimetype, "text/html")
        self.assertIn(f'href="{link}"', html)

        ((filename, content, attachment_type),) = message.attachments
        self.assertEqual(filename, f"boeking_{order.pk}.pdf")
        self.assertEqual(attachment_type, "application/pdf")
        self.assertTrue(content.startswith(b"%PDF"))

        log = EmailLog.objects.get()
        self.assertEqual(log.order, order)
        self.assertEqual(log.kind, "booking_confirmation")
        self.assertEqual(log.recipient, "alex@example.com")
        self.assertEqual(log.status, "sent")
        self.assertEqual(log.subject, message.subject)
        self.assertIsNotNone(log.sent_at)
        self.assertIsNone(log.triggered_by)

    def test_subject_is_one_line_and_not_html_escaped(self):
        order = self.book_and_commit()
        self.assertEqual(
            mail.outbox[0].subject,
            f"Bevestiging boeking #{order.pk} – Hotel & Spa <Amsterdam>",
        )

    def test_body_shows_the_stay_and_the_price(self):
        self.book_and_commit(nights=3)
        body = mail.outbox[0].body
        html = mail.outbox[0].alternatives[0][0]
        self.assertIn("Beste Alex,", body)
        self.assertIn("Hotel & Spa <Amsterdam>", body)
        self.assertIn("Hotel &amp; Spa &lt;Amsterdam&gt;", html)
        self.assertNotIn("<Amsterdam>", html)
        self.assertIn("3 nachten", body)
        self.assertIn("Totaal: € 300,00", body)
        self.assertIn("Je betaalt in het hotel bij aankomst.", body)
        self.assertIn("Kosteloos annuleren kan tot", body)
        self.assertIn("hulp@example.com", body)

    def test_no_mail_when_the_booking_is_rolled_back(self):
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with self.assertRaises(RuntimeError):
                with transaction.atomic():
                    self.book()
                    raise RuntimeError("iets anders ging mis")
        self.assertEqual(callbacks, [])
        self.assertEqual(mail.outbox, [])
        self.assertFalse(Order.objects.exists())
        self.assertFalse(EmailLog.objects.exists())

    def test_smtp_error_keeps_the_booking_and_logs_the_failure(self):
        with mock.patch.object(
            EmailMultiAlternatives, "send", side_effect=SMTPException("server weg")
        ):
            with self.assertLogs("maykinbooking.bookings.emails", "ERROR"):
                order = self.book_and_commit()

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CONFIRMED)
        log = EmailLog.objects.get()
        self.assertEqual(log.status, "failed")
        self.assertEqual(log.error, "SMTPException: server weg")
        self.assertIsNone(log.sent_at)
        self.assertTrue(log.subject.startswith("Bevestiging boeking"))

    def test_rendering_error_is_logged_not_raised(self):
        order = self.book()
        with mock.patch(
            "maykinbooking.bookings.emails.booking_context",
            side_effect=ValueError("kapot"),
        ):
            with self.assertLogs("maykinbooking.bookings.emails", "ERROR"):
                log = send_booking_email(order.pk, "booking_confirmation")
        self.assertEqual(log.status, "failed")
        self.assertEqual(log.subject, "")
        self.assertEqual(mail.outbox, [])

    def test_resend_creates_a_new_log_with_the_staff_member(self):
        order = self.book_and_commit()
        staff = StaffUserFactory()

        log = send_booking_email(order.pk, "booking_confirmation", triggered_by=staff)

        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(EmailLog.objects.count(), 2)
        self.assertEqual(log.triggered_by, staff)
        self.assertEqual(log.status, "sent")
        self.assertEqual(list(order.emails.order_by("pk")).pop(), log)

    def test_unknown_order_or_kind(self):
        with self.assertLogs("maykinbooking.bookings.emails", "ERROR"):
            self.assertIsNone(send_booking_email(999999, "booking_confirmation"))
        order = self.book()
        with self.assertLogs("maykinbooking.bookings.emails", "ERROR"):
            self.assertIsNone(send_booking_email(order.pk, "welkom"))
        self.assertFalse(EmailLog.objects.exists())

    @override_settings(SITE_URL="https://booking.example.com/")
    def test_site_url_with_trailing_slash(self):
        order = self.book()
        self.assertEqual(
            booking_context(order)["my_booking_url"],
            f"https://booking.example.com/boeking/{order.token}/",
        )


class CancellationEmailTests(BookingEmailTestCase):
    def test_cancellation_mail(self):
        order = self.book()
        with self.captureOnCommitCallbacks(execute=True):
            cancel_booking(order.pk, actor=Actor.guest())

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.subject, f"Je boeking #{order.pk} is geannuleerd")
        self.assertIn("is geannuleerd", message.body)
        self.assertIn(f"/boeking/{order.token}/", message.body)
        self.assertEqual(message.attachments, [])
        log = EmailLog.objects.get()
        self.assertEqual((log.kind, log.status), ("booking_cancellation", "sent"))

    def test_staff_cancellation_mail_records_the_staff_member(self):
        order = self.book()
        staff = StaffUserFactory()
        with self.captureOnCommitCallbacks(execute=True):
            cancel_booking(
                order.pk,
                actor=Actor.staff(staff),
                reason="Dubbele boeking",
                enforce_deadline=False,
            )
        self.assertEqual(EmailLog.objects.get().triggered_by, staff)


class BookingContextTests(BookingEmailTestCase):
    def test_context(self):
        order = self.book(nights=2)
        context = booking_context(order)
        self.assertEqual(context["order"], order)
        self.assertEqual(
            [night.date for night in context["nights"]],
            [self.start, self.start + timedelta(days=1)],
        )
        self.assertEqual(context["night_count"], 2)
        self.assertTrue(context["can_cancel"])
        self.assertEqual(context["contact_email"], "hulp@example.com")
        self.assertEqual(
            timezone.localtime(context["cancel_deadline"]).date(),
            self.start - timedelta(days=1),
        )

    def test_pdf(self):
        order = self.book()
        self.assertTrue(render_order_pdf(order).startswith(b"%PDF"))


class EmailLogRobustnessTests(BookingEmailTestCase):
    def test_long_error_is_cut_off_at_500_characters(self):
        order = self.book()
        with mock.patch.object(
            EmailMultiAlternatives, "send", side_effect=SMTPException("x" * 2000)
        ):
            with self.assertLogs("maykinbooking.bookings.emails", "ERROR"):
                log = send_booking_email(order.pk, "booking_confirmation")
        log.refresh_from_db()
        self.assertEqual(len(log.error), MAX_ERROR_LENGTH)
        self.assertEqual(MAX_ERROR_LENGTH, 500)
        self.assertTrue(log.error.startswith("SMTPException: xxx"))

    def test_database_error_of_the_log_does_not_break_the_callers_transaction(self):
        order = self.book()

        def broken_create(**kwargs):
            # A real database error, as on a full disk or a lost column.
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM table_that_does_not_exist")

        with transaction.atomic():
            with mock.patch.object(EmailLog.objects, "create", broken_create):
                with self.assertLogs("maykinbooking.bookings.emails", "ERROR"):
                    self.assertIsNone(
                        send_booking_email(order.pk, "booking_confirmation")
                    )
            # Without its own savepoint PostgreSQL would refuse every next
            # query in this transaction ("current transaction is aborted").
            self.assertEqual(Order.objects.filter(pk=order.pk).count(), 1)


class LastFreeCancellationDayTests(BookingEmailTestCase):
    def test_is_the_day_before_the_deadline_in_amsterdam(self):
        order = self.book()
        deadline = guest_cancellation_deadline(order)  # 00:00 Amsterdam
        expected = self.start - timedelta(days=2)
        self.assertEqual(last_free_cancellation_day(deadline), expected)
        # Also when another time zone is active (UTC: 23:00 the day before).
        for zone in ["UTC", "America/New_York", "Asia/Tokyo"]:
            with self.subTest(zone=zone), timezone.override(zone):
                self.assertEqual(last_free_cancellation_day(deadline), expected)
