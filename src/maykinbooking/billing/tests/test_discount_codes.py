"""Discount codes: checking, using (race-proof counter), giving back."""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from maykinbooking.base.models import OrderStatus
from maykinbooking.base.tests.factories import HotelFactory, OrderFactory, RoomFactory
from maykinbooking.bookings.services.booking import (
    Actor,
    BookingError,
    PaymentMethod,
    PriceChanged,
    cancel_booking,
    expire_order,
)

from ..models import DiscountCode, DiscountKind, DiscountRedemption
from ..services.pricing import (
    InvalidDiscountCode,
    check_discount_code,
    redeem_discount_code,
    release_discount_code,
)
from .factories import DiscountCodeFactory
from .utils import TempMediaMixin, book, in_days


class CheckDiscountCodeTests(TestCase):
    def test_valid_code_as_typed(self):
        code = DiscountCodeFactory(code="ZOMER-26")
        self.assertEqual(check_discount_code("  zomer-26 "), code)

    def test_reasons(self):
        today = timezone.localdate()
        DiscountCodeFactory(code="UIT", is_active=False)
        DiscountCodeFactory(code="LATER", valid_from=today + timedelta(days=1))
        DiscountCodeFactory(code="OUD", valid_until=today - timedelta(days=1))
        DiscountCodeFactory(code="OPGEBRUIKT", max_uses=2, times_used=2)
        for code, reason in [
            ("", "format"),
            ("ab", "format"),
            ("ZOMER 26", "format"),
            ("x" * 41, "format"),
            ("BESTAATNIET", "unknown"),
            ("UIT", "inactive"),
            ("LATER", "not_yet_valid"),
            ("OUD", "expired"),
            ("OPGEBRUIKT", "used_up"),
        ]:
            with self.subTest(code=code):
                with self.assertRaises(InvalidDiscountCode) as error:
                    check_discount_code(code)
                self.assertEqual(error.exception.reason, reason)
                # One message for every reason: no hints about which codes exist.
                self.assertEqual(
                    str(error.exception), "Deze kortingscode is niet geldig."
                )
                self.assertIsInstance(error.exception, BookingError)

    def test_first_and_last_day_are_valid(self):
        today = timezone.localdate()
        DiscountCodeFactory(code="VANDAAG", valid_from=today, valid_until=today)
        self.assertEqual(check_discount_code("VANDAAG").code, "VANDAAG")

    def test_code_is_stored_in_capitals(self):
        code = DiscountCodeFactory(code="herfst")
        code.refresh_from_db()
        self.assertEqual(code.code, "HERFST")

    def test_database_constraints(self):
        for fields in [
            {"value": Decimal("0")},
            {"kind": DiscountKind.PERCENT, "value": Decimal("100.01")},
            {"valid_until": timezone.localdate() - timedelta(days=60)},
            {"max_uses": 1, "times_used": 2},
        ]:
            with self.subTest(fields=fields):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    DiscountCodeFactory(**fields)


class RedeemTests(TestCase):
    def setUp(self):
        self.order = OrderFactory()

    def test_redeem_counts_one_use(self):
        code = DiscountCodeFactory(max_uses=1)
        redemption = redeem_discount_code(code.code, self.order, Decimal("10.00"))
        code.refresh_from_db()
        self.assertEqual(code.times_used, 1)
        self.assertEqual(
            (redemption.code, redemption.order, redemption.amount),
            (code, self.order, Decimal("10.00")),
        )

    def test_last_use_cannot_be_taken_twice(self):
        code = DiscountCodeFactory(max_uses=1)
        redeem_discount_code(code.code, self.order, Decimal("10.00"))
        with self.assertRaises(InvalidDiscountCode) as error:
            redeem_discount_code(code.code, OrderFactory(), Decimal("10.00"))
        self.assertEqual(error.exception.reason, "used_up")

    def test_the_update_checks_again(self):
        # Between the check and the UPDATE another booking took the last use:
        # simulated by a check that still sees a free use.
        code = DiscountCodeFactory(max_uses=1)
        DiscountCode.objects.filter(pk=code.pk).update(times_used=1)
        with mock.patch(
            "maykinbooking.billing.services.pricing.check_discount_code",
            return_value=code,
        ):
            with self.assertRaises(InvalidDiscountCode) as error:
                redeem_discount_code(code.code, self.order, Decimal("10.00"))
        self.assertEqual(error.exception.reason, "used_up")
        self.assertFalse(DiscountRedemption.objects.exists())

    def test_release_gives_the_use_back_once(self):
        code = DiscountCodeFactory(max_uses=1)
        redeem_discount_code(code.code, self.order, Decimal("10.00"))
        self.assertTrue(release_discount_code(self.order))
        self.assertFalse(release_discount_code(self.order))
        code.refresh_from_db()
        self.assertEqual(code.times_used, 0)
        self.assertIsNotNone(DiscountRedemption.objects.get().released_at)


class BookingWithDiscountCodeTests(TempMediaMixin, TestCase):
    def setUp(self):
        self.room = RoomFactory(
            hotel=HotelFactory(is_available=True),
            price=Decimal("100.00"),
            inventory=5,
            is_available=True,
        )

    def test_the_order_keeps_a_snapshot_of_the_discount(self):
        code = DiscountCodeFactory(code="WELKOM", value=Decimal("10"))
        order = book(self.room, in_days(7), in_days(9), discount_code="welkom")
        order.refresh_from_db()
        self.assertEqual(order.discount_code, "WELKOM")
        self.assertEqual(order.code_discount, Decimal("20.00"))
        self.assertEqual(order.total_price, Decimal("180.00"))
        self.assertEqual(order.subtotal_price, Decimal("200.00"))
        self.assertEqual(order.discount_redemption.code, code)
        code.refresh_from_db()
        self.assertEqual(code.times_used, 1)
        # The discount is on the invoice.
        invoice = order.invoices.get()
        self.assertEqual(
            [line.amount_incl for line in invoice.lines.all()],
            [Decimal("100.00"), Decimal("100.00"), Decimal("-20.00")],
        )
        self.assertEqual(invoice.total_incl, Decimal("180.00"))

    def test_invalid_code_books_nothing(self):
        with self.assertRaises(InvalidDiscountCode):
            book(self.room, in_days(7), in_days(9), discount_code="ONBEKEND")
        self.assertFalse(self.room.orders.exists())

    def test_used_up_code_books_nothing(self):
        DiscountCodeFactory(code="EENMALIG", max_uses=1)
        book(self.room, in_days(7), in_days(9), discount_code="EENMALIG")
        with self.assertRaises(InvalidDiscountCode):
            book(self.room, in_days(7), in_days(9), discount_code="EENMALIG")
        self.assertEqual(self.room.orders.count(), 1)

    def test_price_changed_since_the_guest_saw_it(self):
        DiscountCodeFactory(code="WELKOM", value=Decimal("10"))
        with self.assertRaises(PriceChanged) as error:
            book(
                self.room,
                in_days(7),
                in_days(9),
                discount_code="WELKOM",
                expected_total=Decimal("200.00"),
            )
        self.assertEqual(error.exception.quote.total, Decimal("180.00"))
        self.assertFalse(self.room.orders.exists())
        self.assertEqual(DiscountCode.objects.get().times_used, 0)
        order = book(
            self.room,
            in_days(7),
            in_days(9),
            discount_code="WELKOM",
            expected_total=Decimal("180.00"),
        )
        self.assertEqual(order.total_price, Decimal("180.00"))

    def test_expired_order_gives_the_code_back(self):
        code = DiscountCodeFactory(code="EENMALIG", max_uses=1)
        order = book(
            self.room,
            in_days(7),
            in_days(9),
            discount_code="EENMALIG",
            payment_method=PaymentMethod.ONLINE,
        )
        self.assertEqual(order.status, OrderStatus.PENDING_PAYMENT)
        with transaction.atomic():
            order = type(order).objects.select_for_update().get(pk=order.pk)
            self.assertTrue(expire_order(order, actor=Actor.system(), reason="test"))
        code.refresh_from_db()
        self.assertEqual(code.times_used, 0)
        # Free again for the next guest.
        book(self.room, in_days(7), in_days(9), discount_code="EENMALIG")

    def test_cancelled_order_keeps_counting(self):
        code = DiscountCodeFactory(code="EENMALIG", max_uses=1)
        order = book(self.room, in_days(7), in_days(9), discount_code="EENMALIG")
        cancel_booking(order.pk, actor=Actor.guest())
        code.refresh_from_db()
        self.assertEqual(code.times_used, 1)
        self.assertIsNone(DiscountRedemption.objects.get().released_at)
