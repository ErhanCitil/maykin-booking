"""
Races that only PostgreSQL can show: SQLite has no ``SELECT ... FOR UPDATE``
and allows one writer at a time. CI runs these tests.

* invoice numbers without gaps when invoices are issued at the same time, also
  when one of them is rolled back;
* the last use of a discount code taken by two bookings at the same time;
* the webhook and the return URL processing the same payment at the same time.
"""
import threading
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock, skipUnless

from django.db import connection, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from maykinbooking.base.models import Order, OrderStatus
from maykinbooking.base.tests.factories import HotelFactory, OrderFactory, RoomFactory
from maykinbooking.bookings.services.booking import PaymentMethod

from ..models import DiscountCode, Invoice, InvoiceSequence
from ..services.invoicing import issue_invoice
from ..services.payments import start_online_payment, sync_payment
from ..services.pricing import InvalidDiscountCode
from .factories import DiscountCodeFactory
from .utils import book, in_days, pay_at_provider

ATTEMPTS = 5
THREADS = 10

# Mails and PDFs are not what these tests are about (and slow).
NO_MAILS = mock.patch("maykinbooking.bookings.emails.send_booking_email")
NO_PDFS = mock.patch("maykinbooking.billing.services.invoicing.store_invoice_pdf")


def run_at_the_same_time(functions):
    """Start ``functions`` in threads at the same moment; their results."""
    barrier = threading.Barrier(len(functions), timeout=10)
    results = [None] * len(functions)

    def run(index, function):
        try:
            barrier.wait()
            results[index] = function()
        except Exception as exc:
            results[index] = exc
        finally:
            connection.close()  # every thread has its own connection

    threads = [
        threading.Thread(target=run, args=(index, function))
        for index, function in enumerate(functions)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return results


class RolledBack(Exception):
    pass


@skipUnless(connection.vendor == "postgresql", "needs SELECT ... FOR UPDATE")
class InvoiceNumberingRaceTests(TransactionTestCase):
    def issue(self, order_id, year, *, roll_back=False):
        def function():
            with transaction.atomic():
                order = Order.objects.select_for_update().get(pk=order_id)
                invoice = issue_invoice(order, today=date(year, 1, 1))
                if roll_back:
                    raise RolledBack(invoice.number)
            return invoice.number

        return function

    @NO_PDFS
    def test_numbers_without_gaps(self, _store):
        start = timezone.localdate() + timedelta(days=30)
        for attempt in range(ATTEMPTS):
            year = 2040 + attempt
            with self.subTest(attempt=attempt):
                orders = [
                    OrderFactory(
                        start_date=start,
                        end_date=start + timedelta(days=1),
                        status=OrderStatus.CONFIRMED,
                    )
                    for _ in range(THREADS + 1)
                ]
                functions = [self.issue(order.pk, year) for order in orders[:-1]]
                # One more, in the middle, that is rolled back after its number.
                functions.insert(
                    THREADS // 2, self.issue(orders[-1].pk, year, roll_back=True)
                )

                results = run_at_the_same_time(functions)

                self.assertEqual(
                    sum(isinstance(result, RolledBack) for result in results), 1
                )
                numbers = sorted(r for r in results if isinstance(r, str))
                expected = [f"{year}-{n:05d}" for n in range(1, THREADS + 1)]
                self.assertEqual(numbers, expected, results)
                self.assertEqual(
                    sorted(
                        Invoice.objects.filter(year=year).values_list(
                            "number", flat=True
                        )
                    ),
                    expected,
                )
                self.assertEqual(
                    InvoiceSequence.objects.get(year=year).last_number, THREADS
                )


@skipUnless(connection.vendor == "postgresql", "needs SELECT ... FOR UPDATE")
class DiscountCodeRaceTests(TransactionTestCase):
    @NO_MAILS
    @NO_PDFS
    def test_last_use_goes_to_one_booking(self, _store, _send):
        for attempt in range(ATTEMPTS):
            with self.subTest(attempt=attempt):
                code = DiscountCodeFactory(max_uses=1)
                room = RoomFactory(
                    hotel=HotelFactory(is_available=True),
                    inventory=5,
                    is_available=True,
                    price=Decimal("100.00"),
                )

                def booking():
                    return book(room, in_days(7), in_days(9), discount_code=code.code)

                results = run_at_the_same_time([booking, booking])

                self.assertEqual(
                    sorted(type(result).__name__ for result in results),
                    ["InvalidDiscountCode", "Order"],
                    results,
                )
                error = next(r for r in results if isinstance(r, InvalidDiscountCode))
                self.assertEqual(error.reason, "used_up")
                self.assertEqual(DiscountCode.objects.get(pk=code.pk).times_used, 1)
                self.assertEqual(room.orders.count(), 1)


@skipUnless(connection.vendor == "postgresql", "needs SELECT ... FOR UPDATE")
@override_settings(MOLLIE_API_KEY="", PAYMENT_FAKE_PROVIDER=True)
class WebhookRaceTests(TransactionTestCase):
    @NO_MAILS
    @NO_PDFS
    def test_one_payment_is_processed_once(self, _store, send):
        for attempt in range(ATTEMPTS):
            with self.subTest(attempt=attempt):
                room = RoomFactory(
                    hotel=HotelFactory(is_available=True),
                    inventory=1,
                    is_available=True,
                    price=Decimal("100.00"),
                )
                order = book(
                    room, in_days(7), in_days(9), payment_method=PaymentMethod.ONLINE
                )
                payment = start_online_payment(order.pk)
                pay_at_provider(payment)
                send.reset_mock()

                results = run_at_the_same_time([lambda: sync_payment(payment.pk)] * 3)

                self.assertFalse(
                    [r for r in results if isinstance(r, Exception)], results
                )
                order.refresh_from_db()
                self.assertEqual(order.status, OrderStatus.CONFIRMED)
                self.assertEqual(order.invoices.count(), 1)
                self.assertEqual(
                    order.status_changes.filter(
                        new_value=OrderStatus.CONFIRMED
                    ).count(),
                    1,
                )
                self.assertEqual(send.call_count, 1)
