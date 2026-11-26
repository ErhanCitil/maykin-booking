"""
Online payment with the fake provider: starting, syncing (paid, failed, hold,
late and duplicate payments) and the cron jobs.
"""
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from freezegun import freeze_time

from maykinbooking.base.models import (
    EmailKind,
    EmailLog,
    OrderStatus,
    PaymentStatus,
    StatusField,
)
from maykinbooking.base.tests.factories import HotelFactory, RoomFactory
from maykinbooking.bookings.services.booking import (
    OnlinePaymentUnavailable,
    PaymentMethod,
    expire_stale_pending,
)

from ..models import (
    DiscountCode,
    FakeProviderPayment,
    Invoice,
    Payment,
    PaymentState,
    Refund,
    RefundReason,
    RefundState,
)
from ..providers import ProviderError, ProviderPayment
from ..providers.fake import FakeProvider
from ..services.payments import (
    PaymentStartFailed,
    checkout_url_allowed,
    start_online_payment,
    sync_payment,
)
from .factories import DiscountCodeFactory
from .utils import TempMediaMixin, book, commit_hooks, in_days, pay_at_provider

T0 = "2027-03-01T09:00:00Z"


@override_settings(
    MOLLIE_API_KEY="",
    PAYMENT_FAKE_PROVIDER=True,
    SITE_URL="https://booking.example.com",
    PAYMENT_HOLD_MINUTES=15,
)
class PaymentTestCase(TempMediaMixin, TestCase):
    def setUp(self):
        self.room = RoomFactory(
            hotel=HotelFactory(is_available=True),
            price=Decimal("100.00"),
            inventory=1,
            is_available=True,
        )

    def book_online(self, **kwargs):
        return book(
            self.room,
            in_days(7),
            in_days(9),
            payment_method=PaymentMethod.ONLINE,
            **kwargs,
        )

    def start(self, **kwargs):
        order = self.book_online(**kwargs)
        return order, start_online_payment(order.pk)

    def sync(self, payment):
        with commit_hooks():
            payment = sync_payment(payment.pk)
        payment.refresh_from_db()
        payment.order.refresh_from_db()
        return payment


class OnlineBookingTests(PaymentTestCase):
    def test_order_waits_for_payment_without_mail_or_invoice(self):
        with freeze_time(T0), commit_hooks():
            order = self.book_online()
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PENDING_PAYMENT)
        self.assertEqual(order.payment_status, PaymentStatus.OPEN)
        self.assertEqual(
            order.hold_expires_at, datetime(2027, 3, 1, 9, 15, tzinfo=dt_timezone.utc)
        )
        self.assertEqual(mail.outbox, [])
        self.assertFalse(Invoice.objects.exists())

    def test_online_needs_a_provider_and_an_amount(self):
        with override_settings(PAYMENT_FAKE_PROVIDER=False):
            with self.assertRaises(OnlinePaymentUnavailable):
                self.book_online()
        code = DiscountCodeFactory(code="GRATIS", kind="fixed", value=Decimal("500"))
        with self.assertRaises(OnlinePaymentUnavailable):
            self.book_online(discount_code="GRATIS")
        self.assertFalse(self.room.orders.exists())
        code.refresh_from_db()
        self.assertEqual(code.times_used, 0)
        with self.assertRaises(ValueError):
            book(self.room, in_days(7), in_days(9), payment_method="cash")


class StartPaymentTests(PaymentTestCase):
    def test_start(self):
        order, payment = self.start()
        self.assertEqual(payment.order, order)
        self.assertEqual(payment.provider, "fake")
        self.assertEqual(payment.status, PaymentState.OPEN)
        self.assertEqual(payment.amount, Decimal("200.00"))
        self.assertTrue(payment.provider_id.startswith("fake_tr_"))
        self.assertEqual(
            payment.checkout_url,
            f"https://booking.example.com/nep-betaling/{payment.provider_id}/",
        )
        fake = FakeProviderPayment.objects.get(pk=payment.provider_id)
        self.assertEqual(
            fake.redirect_url,
            f"https://booking.example.com/betalen/{payment.reference}/terug/",
        )
        self.assertEqual(fake.metadata["reference"], str(payment.reference))
        self.assertEqual(fake.description, f"Boeking #{order.pk}")

    def test_provider_error_expires_the_order_and_frees_room_and_code(self):
        code = DiscountCodeFactory(code="EENMALIG", max_uses=1)
        order = self.book_online(discount_code="EENMALIG")
        with mock.patch.object(
            FakeProvider,
            "create_payment",
            side_effect=ProviderError("down", retryable=True),
        ), self.assertLogs("maykinbooking.billing", "ERROR"):
            with self.assertRaises(PaymentStartFailed):
                start_online_payment(order.pk)
        order.refresh_from_db()
        payment = Payment.objects.get()
        self.assertEqual(payment.status, PaymentState.ERROR)
        self.assertIn("down", payment.last_error)
        self.assertEqual(order.status, OrderStatus.EXPIRED)
        code.refresh_from_db()
        self.assertEqual(code.times_used, 0)
        # The room can be booked again.
        book(self.room, in_days(7), in_days(9))

    def test_no_provider(self):
        order = self.book_online()
        with override_settings(PAYMENT_FAKE_PROVIDER=False), self.assertLogs(
            "maykinbooking.billing", "ERROR"
        ):
            with self.assertRaises(PaymentStartFailed):
                start_online_payment(order.pk)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.EXPIRED)
        self.assertFalse(Payment.objects.exists())

    def test_checkout_url_on_another_host_is_refused(self):
        order = self.book_online()
        evil = ProviderPayment(
            id="fake_tr_evil",
            status="open",
            amount=Decimal("200.00"),
            currency="EUR",
            checkout_url="https://evil.example.com/pay",
        )
        with mock.patch.object(
            FakeProvider, "create_payment", return_value=evil
        ), self.assertLogs("maykinbooking.billing", "ERROR"):
            with self.assertRaises(PaymentStartFailed):
                start_online_payment(order.pk)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.EXPIRED)

    def test_only_for_orders_that_wait_for_payment(self):
        order = book(self.room, in_days(7), in_days(9))
        with self.assertRaises(PaymentStartFailed):
            start_online_payment(order.pk)

    def test_allowed_checkout_urls(self):
        for url, allowed in [
            ("https://www.mollie.com/checkout/select-method/x", True),
            ("https://mollie.com/x", True),
            ("http://www.mollie.com/x", False),
            ("https://mollie.com.evil.example/x", False),
            ("https://evilmollie.com/x", False),
            ("https://booking.example.com/nep-betaling/x/", True),
            ("//evil.example.com/x", False),
            ("javascript:alert(1)", False),
            ("", False),
            # Security V5: what browsers read differently than urlsplit.
            ("https://evil.example\\.mollie.com/pay", False),
            ("https://evil.com\\@www.mollie.com/", False),
            ("https://user:pw@www.mollie.com/", False),
            ("https://user@www.mollie.com/", False),
            ("https://www.mollie.com:8443/x", False),
            ("https://www.mollie.com:443/x", True),
            ("https://www.mollie.com:abc/x", False),
            ("https://www.mollie.com /x", False),
            ("https://www.mollie.com/x\n", False),
            ("https://www.mollie.com\t/x", False),
            ("https://www.mollie.com\x00/x", False),
            ("https://www.mollie\u3002com/x", False),
            ("https://booking.example.com:444/nep-betaling/x/", False),
            ("http://booking.example.com/nep-betaling/x/", False),
            (None, False),
        ]:
            with self.subTest(url=url):
                self.assertEqual(checkout_url_allowed(url), allowed)

    @override_settings(SITE_URL="http://localhost:8000")
    def test_own_site_as_configured_also_on_http_locally(self):
        self.assertTrue(checkout_url_allowed("http://localhost:8000/nep-betaling/x/"))
        self.assertFalse(checkout_url_allowed("http://localhost:8001/nep-betaling/x/"))
        self.assertFalse(checkout_url_allowed("http://localhost/nep-betaling/x/"))

    def test_an_open_payment_is_given_again(self):
        """A second start (a confirmation sent twice) gives the same payment."""
        order, payment = self.start()
        with mock.patch.object(FakeProvider, "create_payment") as create_payment:
            again = start_online_payment(order.pk)
        create_payment.assert_not_called()
        self.assertEqual(again, payment)
        self.assertEqual(Payment.objects.count(), 1)

    def test_a_failed_payment_is_not_given_again(self):
        order, payment = self.start()
        Payment.objects.filter(pk=payment.pk).update(
            status=PaymentState.FAILED, processed_at=timezone.now()
        )
        again = start_online_payment(order.pk)
        self.assertNotEqual(again, payment)
        self.assertEqual(again.status, PaymentState.OPEN)


class SyncPaymentTests(PaymentTestCase):
    def test_paid_confirms_issues_the_invoice_and_mails(self):
        with freeze_time(T0):
            order, payment = self.start()
        with freeze_time("2027-03-01T09:05:00Z"):
            pay_at_provider(payment)
            payment = self.sync(payment)
        order = payment.order

        self.assertEqual(payment.status, PaymentState.PAID)
        self.assertEqual(payment.method, "ideal")
        self.assertIsNotNone(payment.processed_at)
        self.assertEqual(order.status, OrderStatus.CONFIRMED)
        self.assertEqual(order.payment_status, PaymentStatus.PAID)
        self.assertIsNone(order.hold_expires_at)
        invoice = order.invoices.get()
        self.assertEqual(invoice.total_incl, Decimal("200.00"))
        self.assertEqual(invoice.payment_note, "Betaald via iDEAL op 1 maart 2027.")
        self.assertEqual(
            list(
                order.status_changes.filter(actor_type="system").values_list(
                    "field", "new_value"
                )
            ),
            [
                (StatusField.STATUS, OrderStatus.CONFIRMED),
                (StatusField.PAYMENT_STATUS, PaymentStatus.PAID),
            ],
        )
        (message,) = mail.outbox
        self.assertEqual(
            [name for name, _content, _type in message.attachments],
            [f"boeking_{order.pk}.pdf", invoice.filename],
        )

    def test_syncing_three_times_confirms_once(self):
        order, payment = self.start()
        pay_at_provider(payment)
        for _ in range(3):
            self.sync(payment)
        with mock.patch.object(FakeProvider, "get_payment") as get_payment:
            self.sync(payment)
        get_payment.assert_not_called()
        self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(EmailLog.objects.count(), 1)
        self.assertEqual(order.status_changes.count(), 4)

    def test_open_changes_nothing(self):
        order, payment = self.start()
        payment = self.sync(payment)
        self.assertEqual(payment.status, PaymentState.OPEN)
        self.assertIsNone(payment.processed_at)
        self.assertEqual(payment.order.status, OrderStatus.PENDING_PAYMENT)

    def test_failed_canceled_expired_expire_the_order(self):
        for status in [
            PaymentState.FAILED,
            PaymentState.CANCELED,
            PaymentState.EXPIRED,
        ]:
            with self.subTest(status=status):
                code = DiscountCodeFactory(max_uses=1)
                order, payment = self.start(discount_code=code.code)
                pay_at_provider(payment, status)
                payment = self.sync(payment)
                self.assertEqual(payment.status, status)
                self.assertEqual(payment.order.status, OrderStatus.EXPIRED)
                code.refresh_from_db()
                self.assertEqual(code.times_used, 0)
                self.assertFalse(Invoice.objects.exists())
                self.assertEqual(mail.outbox, [])

    def test_failed_payment_with_another_open_one_keeps_the_order(self):
        order, first = self.start()
        second = Payment.objects.create(
            order=order,
            provider="fake",
            provider_id="fake_tr_second",
            status=PaymentState.OPEN,
            amount=order.total_price,
        )
        self.assertTrue(second.is_open)
        pay_at_provider(first, PaymentState.FAILED)
        self.sync(first)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PENDING_PAYMENT)

    def test_other_amount_is_never_confirmed(self):
        order, payment = self.start()
        FakeProviderPayment.objects.filter(pk=payment.provider_id).update(
            status=PaymentState.PAID, amount=Decimal("1.00")
        )
        with self.assertLogs("maykinbooking.billing", "ERROR"):
            payment = self.sync(payment)
        self.assertEqual(payment.status, PaymentState.ERROR)
        self.assertIn("differs", payment.last_error)
        self.assertEqual(payment.order.status, OrderStatus.EXPIRED)
        self.assertFalse(Invoice.objects.exists())

    def test_provider_error_changes_nothing(self):
        order, payment = self.start()
        with mock.patch.object(
            FakeProvider, "get_payment", side_effect=ProviderError("x", retryable=True)
        ):
            with self.assertRaises(ProviderError):
                sync_payment(payment.pk)
        payment.refresh_from_db()
        self.assertEqual(payment.status, PaymentState.OPEN)


@override_settings(
    MOLLIE_API_KEY="", PAYMENT_FAKE_PROVIDER=True, PAYMENT_HOLD_MINUTES=15
)
class HoldTests(PaymentTestCase):
    """Paid after 14 or 16 minutes, with the room still free or taken."""

    def test_paid_within_the_hold(self):
        with freeze_time(T0):
            order, payment = self.start()
        with freeze_time("2027-03-01T09:14:00Z"):
            pay_at_provider(payment)
            payment = self.sync(payment)
        self.assertEqual(payment.order.status, OrderStatus.CONFIRMED)

    def test_paid_after_the_hold_with_the_room_still_free(self):
        with freeze_time(T0):
            order, payment = self.start()
        with freeze_time("2027-03-01T09:16:00Z"):
            pay_at_provider(payment)
            payment = self.sync(payment)
        self.assertEqual(payment.order.status, OrderStatus.CONFIRMED)
        self.assertEqual(payment.order.payment_status, PaymentStatus.PAID)

    def test_paid_after_the_hold_with_the_room_taken(self):
        with freeze_time(T0):
            order, payment = self.start()
        with freeze_time("2027-03-01T09:15:30Z"):
            other = book(self.room, in_days(7), in_days(9))  # the room is free again
        with freeze_time("2027-03-01T09:16:00Z"):
            mail.outbox.clear()
            pay_at_provider(payment)
            payment = self.sync(payment)
        order = payment.order

        self.assertEqual(order.status, OrderStatus.EXPIRED)
        self.assertEqual(order.payment_status, PaymentStatus.REFUNDED)
        self.assertFalse(order.invoices.exists())
        refund = Refund.objects.get()
        self.assertEqual(refund.payment, payment)
        self.assertEqual(refund.reason, RefundReason.LATE_PAYMENT)
        self.assertEqual(refund.status, RefundState.REFUNDED)
        self.assertEqual(refund.amount, Decimal("200.00"))
        (message,) = mail.outbox
        self.assertEqual(message.subject, f"Terugbetaling boeking #{order.pk}")
        self.assertIn("kwam binnen nadat de reservering was verlopen", message.body)
        self.assertEqual(message.attachments, [])
        self.assertEqual(
            EmailLog.objects.get(order=order).kind, EmailKind.PAYMENT_REFUND
        )
        other.refresh_from_db()
        self.assertEqual(other.status, OrderStatus.CONFIRMED)

    def test_cron_waits_while_the_payment_is_open_and_the_room_stays_ours(self):
        # The hold ends at 09:15; the payment is still open at the provider (the
        # guest is at the bank), so the cron leaves the order alone until 09:30.
        with freeze_time(T0):
            order, payment = self.start()
        with freeze_time("2027-03-01T09:16:00Z"):
            self.assertEqual(expire_stale_pending(), 0)
        with freeze_time("2027-03-01T09:29:00Z"):
            self.assertEqual(expire_stale_pending(), 0)
            pay_at_provider(payment)
            payment = self.sync(payment)
        self.assertEqual(payment.order.status, OrderStatus.CONFIRMED)
        self.assertEqual(payment.order.payment_status, PaymentStatus.PAID)
        self.assertFalse(payment.refunds.exists())

    def test_paid_after_the_cron_expired_the_order(self):
        with freeze_time(T0):
            order, payment = self.start()
        with freeze_time("2027-03-01T09:31:00Z"):
            # After the grace period the order expires after all...
            self.assertEqual(expire_stale_pending(), 1)
            payment.order.refresh_from_db()
            self.assertEqual(payment.order.payment_status, PaymentStatus.UNPAID)
            # ...and a payment that still comes in goes back.
            pay_at_provider(payment)
            payment = self.sync(payment)
        self.assertEqual(payment.order.status, OrderStatus.EXPIRED)
        self.assertEqual(payment.refunds.get().reason, RefundReason.LATE_PAYMENT)
        self.assertEqual(payment.order.payment_status, PaymentStatus.REFUNDED)

    def test_failed_payment_expires_at_once_without_waiting(self):
        with freeze_time(T0):
            order, payment = self.start()
        with freeze_time("2027-03-01T09:16:00Z"):
            pay_at_provider(payment, PaymentState.FAILED)
            # The sync before the expiry already lets it expire.
            expire_stale_pending()
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.EXPIRED)
        self.assertEqual(order.payment_status, PaymentStatus.UNPAID)

    def test_second_payment_of_a_paid_order_is_refunded(self):
        order, first = self.start()
        second = Payment.objects.create(
            order=order,
            provider="fake",
            provider_id=FakeProvider()
            .create_payment(
                amount=order.total_price,
                currency="EUR",
                description="tweede",
                redirect_url="https://booking.example.com/",
                webhook_url=None,
                metadata={},
                idempotency_key="second",
            )
            .id,
            status=PaymentState.OPEN,
            amount=order.total_price,
        )
        pay_at_provider(first)
        self.sync(first)
        mail.outbox.clear()
        pay_at_provider(second)
        second = self.sync(second)

        refund = second.refunds.get()
        self.assertEqual(refund.reason, RefundReason.DUPLICATE)
        self.assertEqual(refund.status, RefundState.REFUNDED)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CONFIRMED)
        # The first payment still stands.
        self.assertEqual(order.payment_status, PaymentStatus.PAID)
        self.assertEqual(len(mail.outbox), 1)


class ExpireStalePendingTests(PaymentTestCase):
    def test_a_paid_order_is_confirmed_instead_of_expired(self):
        with freeze_time(T0):
            order, payment = self.start()
            pay_at_provider(payment)
        with freeze_time("2027-03-01T09:20:00Z"), self.captureOnCommitCallbacks(
            execute=True
        ):
            self.assertEqual(expire_stale_pending(), 0)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CONFIRMED)

    def test_provider_down_waits_up_to_24_hours(self):
        with freeze_time(T0):
            order, payment = self.start()
        down = mock.patch.object(
            FakeProvider, "get_payment", side_effect=ProviderError("x", retryable=True)
        )
        with freeze_time("2027-03-01T10:00:00Z"), down:
            self.assertEqual(expire_stale_pending(), 0)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PENDING_PAYMENT)
        with freeze_time("2027-03-02T09:16:00Z"), down, self.assertLogs(
            "maykinbooking.billing", "ERROR"
        ):
            self.assertEqual(expire_stale_pending(), 1)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.EXPIRED)

    def test_expired_order_gives_its_code_back(self):
        code = DiscountCodeFactory(max_uses=1)
        with freeze_time(T0):
            order = self.book_online(discount_code=code.code)
        with freeze_time("2027-03-01T09:16:00Z"):
            self.assertEqual(expire_stale_pending(), 1)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.EXPIRED)
        self.assertEqual(DiscountCode.objects.get().times_used, 0)


class CommandTests(PaymentTestCase):
    def test_sync_payments(self):
        with freeze_time(T0):
            order, payment = self.start()
            pay_at_provider(payment)
            stdout = StringIO()
            call_command("sync_payments", stdout=stdout)
            # Started less than 2 minutes ago: the guest is still busy.
            self.assertEqual(
                stdout.getvalue().strip(),
                "0 betaling(en) en 0 terugbetaling(en) verwerkt.",
            )
        with freeze_time("2027-03-01T09:03:00Z"), self.captureOnCommitCallbacks(
            execute=True
        ):
            stdout = StringIO()
            call_command("sync_payments", stdout=stdout)
        self.assertEqual(
            stdout.getvalue().strip(), "1 betaling(en) en 0 terugbetaling(en) verwerkt."
        )
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CONFIRMED)

    def test_expire_pending_orders_syncs_first(self):
        with freeze_time(T0):
            order, payment = self.start()
            pay_at_provider(payment)
        with freeze_time("2027-03-01T09:30:00Z"), self.captureOnCommitCallbacks(
            execute=True
        ):
            stdout = StringIO()
            call_command("expire_pending_orders", stdout=stdout)
        self.assertEqual(stdout.getvalue().strip(), "0 boeking(en) verlopen.")
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CONFIRMED)
