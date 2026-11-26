"""
Testpunt 4: two guests book the last room at the same moment.

Only on PostgreSQL: SQLite has no ``SELECT ... FOR UPDATE`` and allows a single
writer, so the race cannot happen (or be tested) there. CI runs this test.
"""
import threading
from datetime import timedelta
from unittest import mock, skipUnless

from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone

from maykinbooking.base.models import Order
from maykinbooking.base.tests.factories import HotelFactory, RoomFactory
from maykinbooking.billing.tests.utils import TempMediaMixin

from ..services.booking import Actor, RoomUnavailable, create_booking

ATTEMPTS = 5

GUEST = {
    "first_name": "Alex",
    "last_name": "de Landgraaf",
    "email": "alex@example.com",
    "address": "Keizersgracht 117",
    "zipcode": "1015CJ",
    "country": "NL",
}


@skipUnless(connection.vendor == "postgresql", "needs SELECT ... FOR UPDATE")
class LastRoomRaceTests(TempMediaMixin, TransactionTestCase):
    def book_at_the_same_time(self, room, start, end):
        barrier = threading.Barrier(2, timeout=10)
        results = []

        def book():
            try:
                barrier.wait()
                results.append(
                    create_booking(
                        room_id=room.pk,
                        start=start,
                        end=end,
                        guest=GUEST,
                        language="nl",
                        actor=Actor.guest(),
                    )
                )
            except Exception as exc:
                results.append(exc)
            finally:
                connection.close()  # every thread has its own connection

        threads = [threading.Thread(target=book) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        return results

    @mock.patch("maykinbooking.bookings.emails.send_booking_email")
    def test_only_one_guest_gets_the_last_room(self, _send):
        start = timezone.localdate() + timedelta(days=7)
        for attempt in range(ATTEMPTS):
            with self.subTest(attempt=attempt):
                room = RoomFactory(
                    hotel=HotelFactory(is_available=True),
                    inventory=1,
                    is_available=True,
                )
                results = self.book_at_the_same_time(
                    room, start, start + timedelta(days=2)
                )

                self.assertEqual(
                    sorted(type(result).__name__ for result in results),
                    ["Order", "RoomUnavailable"],
                    results,
                )
                self.assertEqual(Order.objects.filter(room=room).count(), 1)
                self.assertTrue(
                    any(isinstance(result, RoomUnavailable) for result in results)
                )
