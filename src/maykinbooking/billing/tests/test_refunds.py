"""
Cancelling a paid booking: credit note, refund after the commit, and what
happens when the refund fails (error, retry, taking over an existing refund).
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core import mail
from django.test import TestCase, override_settings

import requests_mock

from maykinbooking.accounts.tests.factories import StaffUserFactory
from maykinbooking.base.models import EmailKind, EmailLog, OrderStatus, PaymentStatus
from maykinbooking.base.tests.factories import HotelFactory, RoomFactory
from maykinbooking.bookings.services.booking import Actor, PaymentMethod, cancel_booking

from ..models import (
    FakeProviderRefund,
    InvoiceKind,
    Payment,
    ProviderName,
    Refund,
    RefundReason,
    RefundState,
)
from ..providers import ProviderError
from ..providers.fake import FakeProvider
from ..services.payments import (
    execute_refund,
    retry_refund,
    start_online_payment,
    sync_payment,
    sync_refunds,
)
from .utils import (
    MOLLIE_API,
    MOLLIE_TEST_KEY,
    TempMediaMixin,
    book,
    commit_hooks,
    in_days,
    mollie_refund,
    mollie_refund_list,
    pay_at_provider,
)

TIMEOUT = ProviderError("Mollie POST /payments/x/refunds: timeout.", retryable=True)


@override_settings(
    MOLLIE_API_KEY="",
    PAYMENT_FAKE_PROVIDER=True,
    SITE_URL="https://booking.example.com",
)
class RefundTestCase(TempMediaMixin, TestCase):
    def setUp(self):
        self.room = RoomFactory(
            hotel=HotelFactory(is_available=True),
            price=Decimal("100.00"),
            inventory=1,
            is_available=True,
        )

    def paid_order(self):
        """A booking paid online: confirmed, paid, with invoice."""
        order = book(
            self.room, in_days(7), in_days(9), payment_method=PaymentMethod.ONLINE
        )
        payment = start_online_payment(order.pk)
        pay_at_provider(payment)
        with commit_hooks():
            sync_payment(payment.pk)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.PAID)
        mail.outbox.clear()
        return order

    def cancel(self, order, actor=None):
        actor = actor or Actor.guest()
        staff = actor.user is not None
        with commit_hooks():
            cancel_booking(
                order.pk,
                actor=actor,
                reason="Op verzoek van de gast" if staff else "",
                enforce_deadline=not staff,
            )
        order.refresh_from_db()
        return order


class CancelPaidBookingTests(RefundTestCase):
    def test_credit_note_refund_and_two_mails(self):
        order = self.paid_order()
        order = self.cancel(order)

        self.assertEqual(order.status, OrderStatus.CANCELLED)
        self.assertEqual(order.payment_status, PaymentStatus.REFUNDED)
        credit_note = order.invoices.get(kind=InvoiceKind.CREDIT_NOTE)
        self.assertEqual(credit_note.total_incl, Decimal("-200.00"))
        self.assertEqual(
            credit_note.payment_note, "Het betaalde bedrag wordt teruggestort."
        )
        refund = Refund.objects.get()
        self.assertEqual(refund.reason, RefundReason.CANCELLATION)
        self.assertEqual(refund.amount, Decimal("200.00"))
        self.assertEqual(refund.status, RefundState.REFUNDED)
        self.assertEqual(refund.attempts, 1)
        self.assertIsNotNone(refund.refunded_at)
        self.assertTrue(refund.provider_id.startswith("fake_re_"))

        cancellation, refund_mail = mail.outbox
        self.assertEqual(
            [name for name, _content, _type in cancellation.attachments],
            [credit_note.filename],
        )
        self.assertEqual(refund_mail.subject, f"Terugbetaling boeking #{order.pk}")
        self.assertIn("€ 200,00", refund_mail.body)
        self.assertEqual(refund_mail.attachments, [])
        self.assertEqual(
            list(EmailLog.objects.filter(order=order).values_list("kind", flat=True)),
            [
                EmailKind.BOOKING_CONFIRMATION,
                EmailKind.BOOKING_CANCELLATION,
                EmailKind.PAYMENT_REFUND,
            ],
        )

    def test_staff_cancellation_records_who_asked_for_the_refund(self):
        staff = StaffUserFactory()
        order = self.paid_order()
        self.cancel(order, actor=Actor.staff(staff))
        self.assertEqual(Refund.objects.get().created_by, staff)

    def test_booking_at_the_hotel_gets_a_credit_note_and_no_refund(self):
        order = book(self.room, in_days(7), in_days(9))
        order = self.cancel(order)
        self.assertEqual(order.payment_status, PaymentStatus.AT_HOTEL)
        self.assertTrue(order.invoices.filter(kind=InvoiceKind.CREDIT_NOTE).exists())
        self.assertFalse(Refund.objects.exists())
        (cancellation,) = mail.outbox
        self.assertIn("Je hoeft niets te betalen.", cancellation.body)

    def test_no_refund_when_the_cancellation_is_rolled_back(self):
        order = self.paid_order()
        with mock.patch(
            "maykinbooking.bookings.services.booking.issue_credit_note",
            side_effect=RuntimeError,
        ), mock.patch.object(FakeProvider, "create_refund") as create_refund:
            with self.assertRaises(RuntimeError), commit_hooks():
                cancel_booking(order.pk, actor=Actor.guest())
        create_refund.assert_not_called()
        self.assertFalse(Refund.objects.exists())
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CONFIRMED)


class FailedRefundTests(RefundTestCase):
    def cancel_with_failing_provider(self, order):
        with mock.patch.object(
            FakeProvider, "create_refund", side_effect=TIMEOUT
        ), self.assertLogs("maykinbooking.billing", "ERROR") as logs:
            order = self.cancel(order)
        self.assertIn("Refund", logs.output[0])
        return order

    def test_order_stays_cancelled_and_paid_with_an_error(self):
        order = self.cancel_with_failing_provider(self.paid_order())

        self.assertEqual(order.status, OrderStatus.CANCELLED)
        self.assertEqual(order.payment_status, PaymentStatus.PAID)
        self.assertTrue(order.invoices.filter(kind=InvoiceKind.CREDIT_NOTE).exists())
        refund = Refund.objects.get()
        self.assertEqual(refund.status, RefundState.ERROR)
        self.assertEqual(refund.attempts, 1)
        self.assertIn("timeout", refund.error)
        # Only the cancellation; the refund mail comes when the money is back.
        (cancellation,) = mail.outbox
        self.assertEqual(cancellation.subject, f"Je boeking #{order.pk} is geannuleerd")

    def test_staff_tries_again(self):
        order = self.cancel_with_failing_provider(self.paid_order())
        mail.outbox.clear()

        with commit_hooks():
            refund = retry_refund(order.pk, actor=Actor.staff(StaffUserFactory()))
        refund.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(refund.status, RefundState.REFUNDED)
        self.assertEqual(refund.attempts, 2)
        self.assertEqual(refund.error, "")
        self.assertEqual(order.payment_status, PaymentStatus.REFUNDED)
        (refund_mail,) = mail.outbox
        self.assertIn("Terugbetaling", refund_mail.subject)
        # Nothing left to retry.
        self.assertIsNone(retry_refund(order.pk, actor=Actor.system()))

    def test_timeout_after_the_refund_was_made_takes_it_over(self):
        order = self.paid_order()
        real_create = FakeProvider.create_refund

        def made_but_timed_out(provider, *args, **kwargs):
            real_create(provider, *args, **kwargs)
            raise TIMEOUT

        with mock.patch.object(
            FakeProvider, "create_refund", autospec=True, side_effect=made_but_timed_out
        ), self.assertLogs("maykinbooking.billing", "ERROR"):
            order = self.cancel(order)
        refund = Refund.objects.get()
        self.assertEqual(refund.status, RefundState.ERROR)

        with mock.patch.object(
            FakeProvider, "create_refund", side_effect=AssertionError("not again")
        ), commit_hooks():
            refund = execute_refund(refund.pk)
        self.assertEqual(refund.status, RefundState.REFUNDED)
        self.assertEqual(FakeProviderRefund.objects.count(), 1)
        self.assertEqual(refund.provider_id, FakeProviderRefund.objects.get().id)

    def test_cron_retries_up_to_five_times(self):
        order = self.cancel_with_failing_provider(self.paid_order())
        refund = Refund.objects.get()
        Refund.objects.filter(pk=refund.pk).update(attempts=5)
        self.assertEqual(sync_refunds(), 0)
        Refund.objects.filter(pk=refund.pk).update(attempts=4)
        with commit_hooks():
            self.assertEqual(sync_refunds(), 1)
        refund.refresh_from_db()
        self.assertEqual(refund.status, RefundState.REFUNDED)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.REFUNDED)

    def test_executing_a_finished_refund_does_nothing(self):
        order = self.cancel(self.paid_order())
        refund = Refund.objects.get()
        with mock.patch.object(FakeProvider, "list_refunds") as list_refunds:
            self.assertEqual(execute_refund(refund.pk).status, RefundState.REFUNDED)
        list_refunds.assert_not_called()
        self.assertEqual(order.status_changes.filter(new_value="refunded").count(), 1)


class SafeCommitHookTests(RefundTestCase):
    """Review S2: work after the commit never breaks the request."""

    def test_an_unexpected_error_of_the_refund_keeps_it_for_the_cron(self):
        order = self.paid_order()
        with mock.patch.object(
            FakeProvider, "list_refunds", side_effect=RuntimeError("bug")
        ), self.assertLogs("maykinbooking.utils.transactions", "ERROR") as logs:
            # No exception for the guest: the booking is cancelled.
            order = self.cancel(order)
        self.assertIn("execute_refund", logs.output[0])
        self.assertEqual(order.status, OrderStatus.CANCELLED)
        refund = Refund.objects.get()
        self.assertEqual(refund.status, RefundState.REQUESTED)
        # The cancellation mail went out before (its own callback).
        self.assertEqual(len(mail.outbox), 1)

        # The cron sends it (requested more than 2 minutes ago).
        Refund.objects.filter(pk=refund.pk).update(
            created_at=refund.created_at - timedelta(minutes=3)
        )
        with commit_hooks():
            self.assertEqual(sync_refunds(), 1)
        refund.refresh_from_db()
        self.assertEqual(refund.status, RefundState.REFUNDED)

    def test_a_failing_mail_or_pdf_after_the_commit_is_logged(self):
        with mock.patch(
            "maykinbooking.bookings.emails.send_booking_email",
            side_effect=RuntimeError("smtp kapot"),
        ), mock.patch(
            "maykinbooking.billing.services.invoicing.store_invoice_pdf",
            side_effect=RuntimeError("weasyprint kapot"),
        ), self.assertLogs(
            "maykinbooking.utils.transactions", "ERROR"
        ) as logs:
            with commit_hooks():
                order = book(self.room, in_days(7), in_days(9))
        self.assertEqual(len(logs.output), 2)
        self.assertTrue(order.invoices.exists())


class ConfirmingPaymentTests(RefundTestCase):
    """Security advice 8 / QA B6: the payment that paid for the order goes back."""

    def test_cancelling_refunds_the_confirming_payment_not_a_duplicate(self):
        order = self.paid_order()
        confirming = Payment.objects.get()
        # A second payment came in afterwards; its refund was refused.
        duplicate = Payment.objects.create(
            order=order,
            provider=ProviderName.FAKE,
            provider_id="fake_tr_duplicate",
            status="paid",
            amount=order.total_price,
            paid_at=confirming.paid_at - timedelta(minutes=1),
            processed_at=confirming.processed_at + timedelta(minutes=1),
        )
        Refund.objects.create(
            payment=duplicate,
            amount=duplicate.amount,
            reason=RefundReason.DUPLICATE,
            status=RefundState.FAILED,
        )

        order = self.cancel(order)

        cancellation = Refund.objects.get(reason=RefundReason.CANCELLATION)
        self.assertEqual(cancellation.payment, confirming)
        self.assertEqual(cancellation.status, RefundState.REFUNDED)
        self.assertEqual(order.payment_status, PaymentStatus.REFUNDED)

    def test_staff_retry_makes_a_new_refund_for_a_refused_duplicate(self):
        order = self.paid_order()
        duplicate = Payment.objects.create(
            order=order,
            provider=ProviderName.FAKE,
            provider_id="fake_tr_duplicate",
            status="paid",
            amount=order.total_price,
        )
        refused = Refund.objects.create(
            payment=duplicate,
            amount=duplicate.amount,
            reason=RefundReason.DUPLICATE,
            status=RefundState.FAILED,
        )
        with mock.patch.object(
            FakeProvider, "list_refunds", return_value=[]
        ), mock.patch.object(FakeProvider, "create_refund") as create_refund:
            create_refund.return_value = mock.Mock(
                id="fake_re_x", status="refunded", amount=duplicate.amount
            )
            with commit_hooks():
                refund = retry_refund(order.pk, actor=Actor.system())
        self.assertNotEqual(refund.pk, refused.pk)
        self.assertEqual((refund.payment, refund.reason), (duplicate, "duplicate"))
        self.assertEqual(create_refund.call_args.args, ("fake_tr_duplicate",))
        order.refresh_from_db()
        # The order itself is still paid (by the first payment).
        self.assertEqual(order.payment_status, PaymentStatus.PAID)

    def test_the_refund_mail_is_about_the_refund_that_was_just_made(self):
        order = self.paid_order()
        duplicate = Payment.objects.create(
            order=order,
            provider=ProviderName.FAKE,
            provider_id="fake_tr_duplicate",
            status="paid",
            amount=Decimal("15.00"),
        )
        late = Refund.objects.create(
            payment=duplicate,
            amount=Decimal("15.00"),
            reason=RefundReason.DUPLICATE,
            status=RefundState.REFUNDED,
        )
        order = self.cancel(order)
        cancellation = Refund.objects.get(reason=RefundReason.CANCELLATION)
        self.assertGreater(cancellation.pk, late.pk)
        refund_mail = mail.outbox[-1]
        self.assertIn("is geannuleerd", refund_mail.body)
        self.assertIn("€ 200,00", refund_mail.body)

        # Sent again for the duplicate: that mail is about the duplicate.
        from maykinbooking.bookings.emails import send_booking_email

        send_booking_email(order.pk, EmailKind.PAYMENT_REFUND, refund_id=late.pk)
        self.assertIn("twee keer betaald", mail.outbox[-1].body)
        self.assertIn("€ 15,00", mail.outbox[-1].body)


@override_settings(MOLLIE_API_KEY=MOLLIE_TEST_KEY, PAYMENT_FAKE_PROVIDER=False)
class MollieRefundTests(RefundTestCase):
    """A refund that Mollie accepts but pays back later, or refuses."""

    def setUp(self):
        super().setUp()
        order = book(self.room, in_days(7), in_days(9))
        order.payment_status = PaymentStatus.PAID
        order.save()
        self.payment = Payment.objects.create(
            order=order,
            provider=ProviderName.MOLLIE,
            provider_id="tr_WDqYK6vllg",
            status="paid",
            amount=order.total_price,
            processed_at=order.created_at,
        )
        self.order = order
        self.refunds_url = f"{MOLLIE_API}/payments/tr_WDqYK6vllg/refunds"

    def test_pending_refund_is_finished_by_the_cron(self):
        with requests_mock.Mocker() as mocker:
            mocker.get(self.refunds_url, json=mollie_refund_list())
            mocker.post(self.refunds_url, json=mollie_refund(status="pending"))
            self.cancel(self.order)
        refund = Refund.objects.get()
        self.assertEqual(refund.status, RefundState.PENDING, refund.error)
        self.assertEqual(refund.provider_id, "re_4qqhO89gsT")
        create = mocker.request_history[1]
        self.assertEqual(create.headers["Idempotency-Key"], str(refund.reference))
        self.assertEqual(create.json()["metadata"]["reference"], str(refund.reference))
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, PaymentStatus.PAID)

        mail.outbox.clear()
        with requests_mock.Mocker() as mocker, commit_hooks():
            mocker.get(
                self.refunds_url,
                json=mollie_refund_list(mollie_refund(status="refunded")),
            )
            self.assertEqual(sync_refunds(), 1)
        refund.refresh_from_db()
        self.assertEqual(refund.status, RefundState.REFUNDED)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, PaymentStatus.REFUNDED)
        self.assertEqual(len(mail.outbox), 1)

    def test_refused_refund_can_be_tried_again_with_a_new_one(self):
        with requests_mock.Mocker() as mocker, self.assertLogs(
            "maykinbooking.billing", "ERROR"
        ):
            mocker.get(self.refunds_url, json=mollie_refund_list())
            mocker.post(self.refunds_url, json=mollie_refund(status="failed"))
            self.cancel(self.order)
        failed = Refund.objects.get()
        self.assertEqual(failed.status, RefundState.FAILED)

        with requests_mock.Mocker() as mocker, commit_hooks():
            mocker.get(self.refunds_url, json=mollie_refund_list())
            mocker.post(
                self.refunds_url,
                json=mollie_refund("re_second", status="refunded"),
            )
            refund = retry_refund(self.order.pk, actor=Actor.system())
        self.assertNotEqual(refund.pk, failed.pk)
        refund.refresh_from_db()
        self.assertEqual(refund.status, RefundState.REFUNDED)
        self.assertEqual(refund.reason, RefundReason.CANCELLATION)
