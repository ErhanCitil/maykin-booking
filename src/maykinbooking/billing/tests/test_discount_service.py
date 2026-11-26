"""
Creating and changing discount codes (``billing.services.discounts``): the code
is fixed after the first use, the maximum stays at or above the uses, and the
counter of uses is never overwritten.
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.core.exceptions import ValidationError
from django.db.models import F
from django.test import TestCase
from django.utils import timezone

from maykinbooking.accounts.tests.factories import StaffUserFactory
from maykinbooking.base.tests.factories import OrderFactory

from ..models import DiscountCode, DiscountKind, DiscountRedemption
from ..services import discounts
from ..services.discounts import (
    create_discount_code,
    discount_code_is_used,
    update_discount_code,
)
from .factories import DiscountCodeFactory

FIELDS = {
    "code": " zomer-27 ",
    "kind": DiscountKind.PERCENT,
    "value": Decimal("10"),
    "valid_from": date(2027, 6, 1),
    "valid_until": date(2027, 8, 31),
    "max_uses": None,
    "is_active": True,
    "description": "Zomeractie",
}


def error_codes(error):
    """``{field: [codes]}`` of a ``ValidationError``."""
    if hasattr(error, "error_dict"):
        return {
            field: [item.code for item in items]
            for field, items in error.error_dict.items()
        }
    return {"__all__": [item.code for item in error.error_list]}


class CreateTests(TestCase):
    def setUp(self):
        self.user = StaffUserFactory()

    def test_create_in_upper_case_by_the_user(self):
        code = create_discount_code(FIELDS, user=self.user)

        code.refresh_from_db()
        self.assertEqual(code.code, "ZOMER-27")
        self.assertEqual(code.value, Decimal("10.00"))
        self.assertEqual(code.created_by, self.user)
        self.assertEqual(code.times_used, 0)
        self.assertIsNone(code.max_uses)

    def test_a_code_exists_once_whatever_the_case(self):
        DiscountCodeFactory(code="ZOMER-27")
        with self.assertRaises(ValidationError) as caught:
            create_discount_code({**FIELDS, "code": "Zomer-27"}, user=self.user)
        self.assertEqual(error_codes(caught.exception), {"code": ["duplicate"]})

    def test_rules(self):
        for changes, expected in (
            ({"code": "AB"}, {"code": ["invalid"]}),
            ({"code": "ZOMER 27"}, {"code": ["invalid"]}),
            ({"value": Decimal("0")}, {"value": ["not_positive"]}),
            ({"value": Decimal("-1")}, {"value": ["not_positive"]}),
            ({"value": Decimal("100.01")}, {"value": ["percent_too_high"]}),
            ({"kind": "gratis"}, {"kind": ["invalid_choice"]}),
            (
                {"valid_until": date(2027, 5, 31)},
                {"valid_until": ["until_before_from"]},
            ),
            ({"max_uses": 0}, {"max_uses": ["min_one"]}),
            (
                {"value": Decimal("0"), "max_uses": 0},
                {"value": ["not_positive"], "max_uses": ["min_one"]},
            ),
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ValidationError) as caught:
                    create_discount_code({**FIELDS, **changes}, user=self.user)
                self.assertEqual(error_codes(caught.exception), expected)
        self.assertFalse(DiscountCode.objects.exists())

    def test_fixed_amount_above_100_and_percentage_of_100(self):
        create_discount_code(
            {
                **FIELDS,
                "code": "VAST",
                "kind": DiscountKind.FIXED,
                "value": Decimal("150"),
            },
            user=self.user,
        )
        create_discount_code({**FIELDS, "value": Decimal("100")}, user=None)
        self.assertEqual(DiscountCode.objects.count(), 2)

    def test_times_used_cannot_be_given(self):
        with self.assertRaises(ValueError):
            create_discount_code({**FIELDS, "times_used": 5}, user=self.user)

    def test_logged_without_the_code(self):
        with self.assertLogs(
            "maykinbooking.billing.services.discounts", "INFO"
        ) as logs:
            code = create_discount_code(FIELDS, user=self.user)
        self.assertEqual(
            logs.output,
            [
                "INFO:maykinbooking.billing.services.discounts:discount code created "
                f"user={self.user.pk} code={code.pk}"
            ],
        )


class UpdateTests(TestCase):
    def setUp(self):
        self.user = StaffUserFactory()

    def test_change_an_unused_code(self):
        code = DiscountCodeFactory(code="OUD", value=Decimal("5.00"))

        update_discount_code(
            code.pk,
            {"code": "nieuw", "kind": DiscountKind.FIXED, "value": Decimal("20")},
            user=self.user,
        )

        code.refresh_from_db()
        self.assertEqual(
            (code.code, code.kind, code.value),
            ("NIEUW", DiscountKind.FIXED, Decimal("20.00")),
        )

    def test_code_kind_and_value_are_fixed_after_the_first_use(self):
        code = DiscountCodeFactory(code="VAST", value=Decimal("10.00"), times_used=1)

        for changes in (
            {"code": "ANDERS"},
            {"kind": DiscountKind.FIXED},
            {"value": Decimal("50")},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ValidationError) as caught:
                    update_discount_code(code.pk, changes, user=self.user)
                self.assertEqual(error_codes(caught.exception), {"__all__": ["locked"]})

        # The same values (and the rest) are fine.
        update_discount_code(
            code.pk,
            {"code": "vast", "value": Decimal("10"), "is_active": False},
            user=self.user,
        )
        code.refresh_from_db()
        self.assertEqual(
            (code.code, code.value, code.is_active), ("VAST", Decimal("10.00"), False)
        )

    def test_a_released_use_still_counts_as_used(self):
        code = DiscountCodeFactory()
        self.assertFalse(discount_code_is_used(code))
        DiscountRedemption.objects.create(
            code=code,
            order=OrderFactory(),
            amount=Decimal("10.00"),
            released_at=timezone.now(),
        )
        self.assertTrue(discount_code_is_used(code))
        with self.assertRaises(ValidationError):
            update_discount_code(code.pk, {"value": Decimal("20")}, user=self.user)

    def test_maximum_not_below_the_uses(self):
        code = DiscountCodeFactory(max_uses=10, times_used=4)

        with self.assertRaises(ValidationError) as caught:
            update_discount_code(code.pk, {"max_uses": 3}, user=self.user)
        self.assertEqual(error_codes(caught.exception), {"max_uses": ["below_uses"]})
        self.assertEqual(
            caught.exception.message_dict["max_uses"],
            ["De code is al 4 keer gebruikt; het maximum kan niet lager zijn."],
        )

        update_discount_code(code.pk, {"max_uses": 4}, user=self.user)
        code.refresh_from_db()
        self.assertEqual(code.max_uses, 4)
        update_discount_code(code.pk, {"max_uses": None}, user=self.user)
        code.refresh_from_db()
        self.assertIsNone(code.max_uses)

    def test_the_counter_is_never_written(self):
        """A guest uses the code after it was read: the use is kept."""
        code = DiscountCodeFactory(max_uses=10, times_used=4)
        real_validate = discounts._validate

        def guest_books_meanwhile(locked):
            DiscountCode.objects.filter(pk=locked.pk).update(
                times_used=F("times_used") + 1
            )
            return real_validate(locked)

        with mock.patch.object(
            discounts, "_validate", side_effect=guest_books_meanwhile
        ):
            update_discount_code(code.pk, {"description": "Nieuw"}, user=self.user)

        code.refresh_from_db()
        self.assertEqual((code.times_used, code.description), (5, "Nieuw"))

    def test_the_rules_use_the_current_uses(self):
        code = DiscountCodeFactory(max_uses=10, times_used=4)
        DiscountCode.objects.filter(pk=code.pk).update(times_used=6)
        # The caller read 4 uses; the service checks against 6.
        with self.assertRaises(ValidationError):
            update_discount_code(code.pk, {"max_uses": 5}, user=self.user)

    def test_first_use_since_reading_fixes_the_code(self):
        code = DiscountCodeFactory(code="NIEUW", value=Decimal("10.00"))
        DiscountCode.objects.filter(pk=code.pk).update(times_used=1)
        with self.assertRaises(ValidationError) as caught:
            update_discount_code(code.pk, {"value": Decimal("50")}, user=self.user)
        self.assertEqual(error_codes(caught.exception), {"__all__": ["locked"]})

    def test_the_row_is_locked(self):
        code = DiscountCodeFactory()
        with mock.patch.object(
            DiscountCode.objects,
            "select_for_update",
            wraps=DiscountCode.objects.select_for_update,
        ) as select_for_update:
            update_discount_code(code.pk, {"is_active": False}, user=self.user)
        select_for_update.assert_called_once_with()

    def test_rules_on_change(self):
        code = DiscountCodeFactory(
            valid_from=date(2027, 1, 1), valid_until=date(2027, 12, 31)
        )
        DiscountCodeFactory(code="BEZET")
        for changes, expected in (
            (
                {"valid_until": date(2026, 12, 31)},
                {"valid_until": ["until_before_from"]},
            ),
            ({"code": "bezet"}, {"code": ["duplicate"]}),
            ({"value": Decimal("101")}, {"value": ["percent_too_high"]}),
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ValidationError) as caught:
                    update_discount_code(code.pk, changes, user=self.user)
                self.assertEqual(error_codes(caught.exception), expected)

    def test_unknown_code_and_unknown_fields(self):
        with self.assertRaises(DiscountCode.DoesNotExist):
            update_discount_code(999, {"is_active": False}, user=self.user)
        code = DiscountCodeFactory()
        with self.assertRaises(ValueError):
            update_discount_code(code.pk, {"times_used": 0}, user=self.user)
        with self.assertRaises(ValueError):
            update_discount_code(code.pk, {"created_by": None}, user=self.user)

    def test_change_is_logged(self):
        code = DiscountCodeFactory(valid_until=timezone.localdate() + timedelta(days=5))
        with self.assertLogs(
            "maykinbooking.billing.services.discounts", "INFO"
        ) as logs:
            update_discount_code(
                code.pk, {"is_active": False, "description": "x"}, user=self.user
            )
        self.assertEqual(
            logs.output,
            [
                "INFO:maykinbooking.billing.services.discounts:discount code changed "
                f"user={self.user.pk} code={code.pk} fields=description,is_active"
            ],
        )
