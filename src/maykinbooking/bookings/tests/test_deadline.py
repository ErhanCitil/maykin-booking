"""
Testpunt 6: guests can cancel for free until 00:00 Europe/Amsterdam on the day
before arrival. All times in these tests are UTC.
"""
from datetime import date, datetime, timezone as dt_timezone

from django.test import TestCase

from freezegun import freeze_time

from maykinbooking.base.models import OrderStatus
from maykinbooking.base.tests.factories import OrderFactory

from ..services.booking import can_guest_cancel, guest_cancellation_deadline


def utc(*args):
    return datetime(*args, tzinfo=dt_timezone.utc)


class GuestCancellationDeadlineTests(TestCase):
    def order(self, start_date, **fields):
        return OrderFactory(start_date=start_date, nights=False, **fields)

    def test_deadline_is_midnight_amsterdam_on_the_day_before_arrival(self):
        order = self.order(date(2027, 3, 10))
        self.assertEqual(guest_cancellation_deadline(order), utc(2027, 3, 8, 23, 0))

    def test_guest_can_cancel_until_the_deadline(self):
        order = self.order(date(2027, 3, 10))
        with freeze_time(utc(2027, 3, 8, 22, 59, 59)):
            self.assertTrue(can_guest_cancel(order))
        with freeze_time(utc(2027, 3, 8, 23, 0, 0)):
            self.assertFalse(can_guest_cancel(order))

    def test_now_can_be_passed(self):
        order = self.order(date(2027, 3, 10))
        self.assertTrue(can_guest_cancel(order, now=utc(2027, 3, 8, 22, 59, 59)))
        self.assertFalse(can_guest_cancel(order, now=utc(2027, 3, 8, 23, 0, 0)))

    def test_summer_time_starts_on_28_march_2027(self):
        # The deadline for arrival on the 29th is still in winter time (UTC+1),
        # for arrival on the 30th it is in summer time (UTC+2).
        self.assertEqual(
            guest_cancellation_deadline(self.order(date(2027, 3, 29))),
            utc(2027, 3, 27, 23, 0),
        )
        self.assertEqual(
            guest_cancellation_deadline(self.order(date(2027, 3, 30))),
            utc(2027, 3, 28, 22, 0),
        )

    def test_summer_time_ends_on_31_october_2027(self):
        # Midnight on the 31st is before the switch at 03:00, so still UTC+2.
        self.assertEqual(
            guest_cancellation_deadline(self.order(date(2027, 11, 1))),
            utc(2027, 10, 30, 22, 0),
        )
        self.assertEqual(
            guest_cancellation_deadline(self.order(date(2027, 11, 2))),
            utc(2027, 10, 31, 23, 0),
        )

    def test_only_confirmed_orders_can_be_cancelled_by_the_guest(self):
        for status in [
            OrderStatus.PENDING_PAYMENT,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
        ]:
            with self.subTest(status=status):
                order = self.order(date(2027, 3, 10), status=status)
                self.assertFalse(can_guest_cancel(order, now=utc(2027, 3, 1, 12, 0)))
