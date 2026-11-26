import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from freezegun import freeze_time

from maykinbooking.accounts.tests.factories import StaffUserFactory
from maykinbooking.base.models import (
    Order,
    OrderNight,
    OrderStatus,
    OrderStatusChange,
    PaymentStatus,
    Room,
    RoomBlock,
)
from maykinbooking.base.tests.factories import (
    HotelFactory,
    OrderFactory,
    RoomBlockFactory,
    RoomFactory,
)

from ..services.booking import (
    Actor,
    CancellationNotAllowed,
    InvalidTransition,
    RoomUnavailable,
    cancel_booking,
    create_booking,
    create_room_block,
    expire_stale_pending,
    quote,
    set_payment_status,
    transition,
)

GUEST = {
    "first_name": "Alex",
    "last_name": "de Landgraaf",
    "email": "alex@example.com",
    "address": "Keizersgracht 117",
    "zipcode": "1015CJ",
    "country": "NL",
}

SEND_EMAIL = "maykinbooking.bookings.emails.send_booking_email"


def in_days(days):
    return timezone.localdate() + timedelta(days=days)


class BookingTestCase(TestCase):
    def setUp(self):
        self.hotel = HotelFactory(is_available=True)
        self.room = RoomFactory(
            hotel=self.hotel, price=Decimal("100.00"), inventory=1, is_available=True
        )

    def book(self, start=7, end=10, *, room=None, actor=None):
        return create_booking(
            room_id=(room or self.room).pk,
            start=in_days(start),
            end=in_days(end),
            guest=GUEST,
            language="nl",
            actor=actor or Actor.guest(),
        )


class CreateBookingTests(BookingTestCase):
    def test_creates_a_confirmed_order_with_a_price_snapshot(self):
        order = self.book(7, 10)

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CONFIRMED)
        self.assertEqual(order.payment_status, PaymentStatus.AT_HOTEL)
        self.assertEqual(order.hotel, self.hotel)
        self.assertEqual(order.room, self.room)
        self.assertEqual((order.start_date, order.end_date), (in_days(7), in_days(10)))
        self.assertEqual(order.total_price, Decimal("300.00"))
        self.assertEqual(order.language, "nl")
        for field, value in GUEST.items():
            self.assertEqual(getattr(order, field), value)
        self.assertEqual(
            list(order.nights.values_list("date", "price")),
            [(in_days(day), Decimal("100.00")) for day in (7, 8, 9)],
        )

    def test_logs_status_and_payment_status(self):
        order = self.book()
        self.assertEqual(
            list(
                order.status_changes.values_list(
                    "field", "old_value", "new_value", "actor_type", "actor_label"
                )
            ),
            [
                ("status", "", "confirmed", "guest", "Gast"),
                ("payment_status", "", "at_hotel", "guest", "Gast"),
            ],
        )

    def test_sends_the_confirmation_after_the_commit(self):
        with mock.patch(SEND_EMAIL) as send:
            with self.captureOnCommitCallbacks(execute=True):
                order = self.book()
                send.assert_not_called()
        send.assert_called_once_with(
            order.pk, "booking_confirmation", triggered_by=None
        )

    def test_last_room_cannot_be_booked_twice(self):
        self.book(7, 10)
        with self.assertRaises(RoomUnavailable):
            self.book(9, 11)
        self.assertEqual(Order.objects.count(), 1)

    def test_other_dates_and_more_inventory_can_be_booked(self):
        self.book(7, 10)
        self.book(10, 12)  # arrives on the day the first guest leaves
        Room.objects.filter(pk=self.room.pk).update(inventory=2)
        self.book(7, 10)
        self.assertEqual(Order.objects.count(), 3)

    def test_unbookable_rooms(self):
        cases = {
            "room not available": RoomFactory(hotel=self.hotel, is_available=False),
            "hotel not available": RoomFactory(hotel=HotelFactory(is_available=False)),
            "inventory 0": RoomFactory(hotel=self.hotel, inventory=0),
        }
        RoomBlockFactory(room=self.room, start_date=in_days(1), end_date=in_days(30))
        cases["blocked"] = self.room
        for case, room in cases.items():
            with self.subTest(case):
                with self.assertRaises(RoomUnavailable):
                    self.book(room=room)
        self.assertFalse(Order.objects.exists())
        self.assertFalse(OrderStatusChange.objects.exists())

    def test_unknown_room(self):
        with self.assertRaises(RoomUnavailable):
            create_booking(
                room_id=999999,
                start=in_days(7),
                end=in_days(8),
                guest=GUEST,
                language="nl",
                actor=Actor.guest(),
            )

    def test_invalid_period(self):
        for start, end in [(-1, 2), (5, 5), (1, 40)]:
            with self.subTest(start=start, end=end):
                with self.assertRaises(ValidationError):
                    self.book(start, end)
        self.assertFalse(Order.objects.exists())

    def test_price_change_does_not_change_existing_orders(self):
        """Testpunt 9: the order keeps the price it was booked for."""
        order = self.book(7, 9)
        Room.objects.filter(pk=self.room.pk).update(price=Decimal("250.00"))

        order.refresh_from_db()
        self.assertEqual(order.total_price, Decimal("200.00"))
        self.assertEqual(
            set(order.nights.values_list("price", flat=True)), {Decimal("100.00")}
        )
        self.room.refresh_from_db()
        self.assertEqual(quote(self.room, in_days(7), in_days(9)).total, Decimal("500"))


class IdempotentBookingTests(BookingTestCase):
    """``create_booking(token=...)``: a booking that is sent twice books once."""

    def setUp(self):
        super().setUp()
        Room.objects.filter(pk=self.room.pk).update(inventory=5)
        self.token = uuid.uuid4()

    def book_with_token(self, start=7, end=10):
        return create_booking(
            room_id=self.room.pk,
            start=in_days(start),
            end=in_days(end),
            guest=GUEST,
            language="nl",
            actor=Actor.guest(),
            token=self.token,
        )

    def test_token_becomes_the_order_token(self):
        order = self.book_with_token()
        order.refresh_from_db()
        self.assertEqual(order.token, self.token)

    def test_without_token_the_order_gets_a_new_one(self):
        first, second = self.book(), self.book()
        self.assertNotEqual(first.token, second.token)
        self.assertIsNotNone(first.token)

    def test_same_token_returns_the_existing_order_without_booking_again(self):
        with mock.patch(SEND_EMAIL) as send:
            with self.captureOnCommitCallbacks(execute=True):
                first = self.book_with_token()
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                second = self.book_with_token()

        self.assertEqual(second.pk, first.pk)
        self.assertEqual(callbacks, [])
        send.assert_called_once()
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(OrderNight.objects.count(), 3)
        self.assertEqual(OrderStatusChange.objects.count(), 2)

    def test_same_token_wins_over_a_full_room(self):
        # The second request of a double click must not say "no room free".
        Room.objects.filter(pk=self.room.pk).update(inventory=1)
        first = self.book_with_token()
        self.assertEqual(self.book_with_token().pk, first.pk)


class QuoteTests(BookingTestCase):
    def test_price_per_night_and_total(self):
        result = quote(self.room, in_days(1), in_days(4))
        self.assertEqual(
            result.nights,
            [(in_days(day), Decimal("100.00")) for day in (1, 2, 3)],
        )
        self.assertEqual(result.total, Decimal("300.00"))


class ActorTests(TestCase):
    def test_labels(self):
        self.assertEqual(Actor.guest().label, "Gast")
        self.assertEqual(Actor.system().label, "Systeem")
        named = StaffUserFactory(first_name="Sam", last_name="Jansen")
        self.assertEqual(Actor.staff(named).label, "Sam Jansen")
        unnamed = StaffUserFactory(username="balie", first_name="", last_name="")
        self.assertEqual(Actor.staff(unnamed).label, "balie")

    def test_staff_needs_a_user(self):
        with self.assertRaises(ValueError):
            Actor.staff(None)


class TransitionTests(TestCase):
    def setUp(self):
        self.staff = StaffUserFactory(first_name="Sam", last_name="Jansen")

    def test_allowed_transitions(self):
        allowed = [
            (OrderStatus.PENDING_PAYMENT, OrderStatus.CONFIRMED),
            (OrderStatus.PENDING_PAYMENT, OrderStatus.EXPIRED),
            (OrderStatus.CONFIRMED, OrderStatus.CANCELLED),
        ]
        for old, new in allowed:
            with self.subTest(old=old, new=new):
                order = OrderFactory(status=old)
                self.assertTrue(
                    transition(order, new, actor=Actor.staff(self.staff), reason="x")
                )
                order.refresh_from_db()
                self.assertEqual(order.status, new)
                change = order.status_changes.get()
                self.assertEqual(
                    (change.field, change.old_value, change.new_value),
                    ("status", old, new),
                )
                self.assertEqual(change.actor_type, "staff")
                self.assertEqual(change.actor_user, self.staff)
                self.assertEqual(change.actor_label, "Sam Jansen")
                self.assertEqual(change.reason, "x")

    def test_same_status_changes_nothing(self):
        order = OrderFactory(status=OrderStatus.CONFIRMED)
        self.assertFalse(transition(order, "confirmed", actor=Actor.system()))
        self.assertFalse(order.status_changes.exists())

    def test_invalid_transitions(self):
        invalid = [
            (OrderStatus.CONFIRMED, OrderStatus.PENDING_PAYMENT),
            (OrderStatus.CONFIRMED, OrderStatus.EXPIRED),
            (OrderStatus.CANCELLED, OrderStatus.CONFIRMED),
            (OrderStatus.EXPIRED, OrderStatus.CONFIRMED),
            (OrderStatus.PENDING_PAYMENT, OrderStatus.CANCELLED),
        ]
        for old, new in invalid:
            with self.subTest(old=old, new=new):
                order = OrderFactory(status=old)
                with self.assertRaises(InvalidTransition):
                    transition(order, new, actor=Actor.system())
                order.refresh_from_db()
                self.assertEqual(order.status, old)
                self.assertFalse(order.status_changes.exists())


class SetPaymentStatusTests(TestCase):
    def test_change_is_logged(self):
        order = OrderFactory()
        self.assertTrue(
            set_payment_status(order, PaymentStatus.PAID, actor=Actor.system())
        )
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "paid")
        self.assertEqual(
            list(order.status_changes.values_list("field", "old_value", "new_value")),
            [("payment_status", "at_hotel", "paid")],
        )

    def test_same_payment_status_changes_nothing(self):
        order = OrderFactory()
        self.assertFalse(set_payment_status(order, "at_hotel", actor=Actor.system()))
        self.assertFalse(order.status_changes.exists())

    def test_unknown_payment_status(self):
        with self.assertRaises(ValueError):
            set_payment_status(OrderFactory(), "gratis", actor=Actor.system())


class CancelBookingTests(BookingTestCase):
    """Testpunt 7. The deadline itself is tested in test_deadline."""

    def setUp(self):
        super().setUp()
        self.staff = StaffUserFactory()
        self.order = self.book(7, 9)

    def cancel(self, **kwargs):
        kwargs.setdefault("actor", Actor.guest())
        with mock.patch(SEND_EMAIL) as send:
            with self.captureOnCommitCallbacks(execute=True):
                result = cancel_booking(self.order.pk, **kwargs)
        return result, send

    def test_guest_cancels_before_the_deadline(self):
        (order, changed), send = self.cancel()
        self.assertTrue(changed)
        self.assertEqual(order.status, OrderStatus.CANCELLED)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.CANCELLED)
        change = self.order.status_changes.get(old_value="confirmed")
        self.assertEqual((change.new_value, change.actor_type), ("cancelled", "guest"))
        send.assert_called_once_with(
            self.order.pk, "booking_cancellation", triggered_by=None
        )

    def test_cancelling_twice_gives_one_log_and_one_mail(self):
        _result, first_send = self.cancel()
        (order, changed), second_send = self.cancel()
        self.assertFalse(changed)
        self.assertEqual(order.status, OrderStatus.CANCELLED)
        self.assertEqual(
            self.order.status_changes.filter(new_value="cancelled").count(), 1
        )
        first_send.assert_called_once()
        second_send.assert_not_called()

    def test_cancelled_room_is_free_again(self):
        self.cancel()
        self.book(7, 9)
        self.assertEqual(Order.objects.filter(status="confirmed").count(), 1)

    def test_guest_cannot_cancel_after_the_deadline(self):
        after_deadline = timezone.now() + timedelta(days=7)
        with freeze_time(after_deadline):
            with self.assertRaises(CancellationNotAllowed):
                self.cancel()
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.CONFIRMED)

    def test_staff_can_cancel_after_the_deadline_with_a_reason(self):
        after_deadline = timezone.now() + timedelta(days=7)
        with freeze_time(after_deadline):
            (order, changed), send = self.cancel(
                actor=Actor.staff(self.staff),
                reason="  Hotel gesloten  ",
                enforce_deadline=False,
            )
        self.assertTrue(changed)
        change = self.order.status_changes.get(new_value="cancelled")
        self.assertEqual(change.actor_user, self.staff)
        self.assertEqual(change.reason, "Hotel gesloten")
        send.assert_called_once_with(
            self.order.pk, "booking_cancellation", triggered_by=self.staff
        )

    def test_staff_needs_a_reason(self):
        for reason in ["", "   "]:
            with self.subTest(reason=reason):
                with self.assertRaises(ValueError):
                    self.cancel(
                        actor=Actor.staff(self.staff),
                        reason=reason,
                        enforce_deadline=False,
                    )
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.CONFIRMED)

    def test_expired_order_cannot_be_cancelled(self):
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.EXPIRED)
        with self.assertRaises(InvalidTransition):
            self.cancel(
                actor=Actor.staff(self.staff), reason="x", enforce_deadline=False
            )
        with self.assertRaises(CancellationNotAllowed):
            self.cancel()

    def test_unknown_order(self):
        with self.assertRaises(Order.DoesNotExist):
            cancel_booking(999999, actor=Actor.guest())

    def test_nobody_can_cancel_once_the_stay_is_over(self):
        Order.objects.filter(pk=self.order.pk).update(
            start_date=in_days(-3), end_date=in_days(-1)
        )
        cases = {
            "guest": {"actor": Actor.guest()},
            "staff": {
                "actor": Actor.staff(self.staff),
                "reason": "Te laat gemeld",
                "enforce_deadline": False,
            },
        }
        for who, kwargs in cases.items():
            with self.subTest(who):
                with self.assertRaises(CancellationNotAllowed) as error:
                    self.cancel(**kwargs)
                self.assertIn("voorbij", str(error.exception))
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.CONFIRMED)
        self.assertFalse(
            self.order.status_changes.filter(new_value="cancelled").exists()
        )

    def test_staff_can_still_cancel_on_the_day_of_departure(self):
        # A no-show for one night: arrived (not) yesterday, leaves today.
        Order.objects.filter(pk=self.order.pk).update(
            start_date=in_days(-1), end_date=in_days(0)
        )
        (_order, changed), send = self.cancel(
            actor=Actor.staff(self.staff), reason="No-show", enforce_deadline=False
        )
        self.assertTrue(changed)
        send.assert_called_once()
        # The guest could not: the free cancellation deadline has long passed.

    def test_stay_is_over_from_the_day_after_departure(self):
        Order.objects.filter(pk=self.order.pk).update(
            start_date=in_days(-2), end_date=in_days(-1)
        )
        with self.assertRaises(CancellationNotAllowed):
            self.cancel(
                actor=Actor.staff(self.staff), reason="x", enforce_deadline=False
            )

    def test_guest_cannot_cancel_on_the_day_of_departure(self):
        Order.objects.filter(pk=self.order.pk).update(
            start_date=in_days(-1), end_date=in_days(0)
        )
        with self.assertRaises(CancellationNotAllowed) as error:
            self.cancel()
        self.assertIn("kosteloos", str(error.exception))

    def test_staff_can_cancel_on_the_last_night(self):
        Order.objects.filter(pk=self.order.pk).update(
            start_date=in_days(-2), end_date=in_days(1)
        )
        (_order, changed), _send = self.cancel(
            actor=Actor.staff(self.staff),
            reason="Vertrekt eerder",
            enforce_deadline=False,
        )
        self.assertTrue(changed)

    def test_already_cancelled_is_checked_before_the_stay(self):
        self.cancel()
        Order.objects.filter(pk=self.order.pk).update(
            start_date=in_days(-3), end_date=in_days(-1)
        )
        (order, changed), send = self.cancel()
        self.assertFalse(changed)
        self.assertEqual(order.status, OrderStatus.CANCELLED)
        send.assert_not_called()


class CreateRoomBlockTests(BookingTestCase):
    def setUp(self):
        super().setUp()
        self.staff = StaffUserFactory()

    def test_creates_the_block(self):
        block, overbooked = create_room_block(
            self.room.pk,
            start=in_days(3),
            end=in_days(5),
            quantity=1,
            reason="Schilderwerk",
            user=self.staff,
        )
        self.assertEqual(overbooked, [])
        block.refresh_from_db()
        self.assertEqual(
            (block.room, block.start_date, block.end_date, block.quantity),
            (self.room, in_days(3), in_days(5), 1),
        )
        self.assertEqual((block.reason, block.created_by), ("Schilderwerk", self.staff))
        with self.assertRaises(RoomUnavailable):
            self.book(4, 6)

    def test_block_over_a_booking_is_created_with_a_warning(self):
        self.book(7, 10)
        block, overbooked = create_room_block(
            self.room.pk,
            start=in_days(9),
            end=in_days(12),
            quantity=1,
            reason="",
            user=None,
        )
        self.assertTrue(RoomBlock.objects.filter(pk=block.pk).exists())
        self.assertEqual(overbooked, [(in_days(9), 2)])
        self.assertEqual(Order.objects.get().status, OrderStatus.CONFIRMED)

    def test_invalid_block(self):
        cases = [
            ("end_before_start", {"start": in_days(5), "end": in_days(5)}),
            ("quantity", {"quantity": 0}),
        ]
        for code, overrides in cases:
            kwargs = {
                "start": in_days(3),
                "end": in_days(5),
                "quantity": 1,
                "reason": "",
                "user": None,
                **overrides,
            }
            with self.subTest(code):
                with self.assertRaises(ValidationError) as error:
                    create_room_block(self.room.pk, **kwargs)
                self.assertEqual(error.exception.code, code)
        self.assertFalse(RoomBlock.objects.exists())


class ExpireStalePendingTests(TestCase):
    now = datetime(2027, 3, 1, 12, 0, tzinfo=dt_timezone.utc)

    def pending(self, minutes_left):
        hold = (
            None if minutes_left is None else self.now + timedelta(minutes=minutes_left)
        )
        return OrderFactory(
            status=OrderStatus.PENDING_PAYMENT,
            payment_status=PaymentStatus.OPEN,
            hold_expires_at=hold,
        )

    def test_expires_pending_orders_whose_hold_has_passed(self):
        stale = [self.pending(-1), self.pending(0), self.pending(None)]
        active = self.pending(1)
        confirmed = OrderFactory()

        self.assertEqual(expire_stale_pending(now=self.now), 3)

        for order in stale:
            order.refresh_from_db()
            self.assertEqual(order.status, OrderStatus.EXPIRED)
            change = order.status_changes.get()
            self.assertEqual(
                (change.old_value, change.new_value, change.actor_type),
                ("pending_payment", "expired", "system"),
            )
        for order in [active, confirmed]:
            order.refresh_from_db()
            self.assertNotEqual(order.status, OrderStatus.EXPIRED)
        self.assertEqual(expire_stale_pending(now=self.now), 0)

    def test_management_command(self):
        self.pending(-1)
        stdout = StringIO()
        with freeze_time(self.now):
            call_command("expire_pending_orders", stdout=stdout)
        self.assertEqual(stdout.getvalue().strip(), "1 boeking(en) verlopen.")
        self.assertTrue(Order.objects.filter(status=OrderStatus.EXPIRED).exists())


class OrderNightsTests(BookingTestCase):
    def test_one_night_row_per_night(self):
        order = self.book(1, 31)
        self.assertEqual(OrderNight.objects.filter(order=order).count(), 30)
        self.assertEqual(order.total_price, Decimal("3000.00"))
