"""
Discount codes in the dashboard: the list, adding and changing them, and what
is fixed once a code has been used.
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.db.models import F
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from maykinbooking.accounts.tests.factories import ManagerUserFactory
from maykinbooking.base.tests.factories import OrderFactory
from maykinbooking.billing.models import DiscountCode, DiscountKind, DiscountRedemption
from maykinbooking.billing.tests.factories import DiscountCodeFactory

from ..forms import DiscountCodeForm


class DiscountTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = ManagerUserFactory()

    def setUp(self):
        self.client.force_login(self.staff)
        self.today = timezone.localdate()

    def data(self, **values):
        data = {
            "code": "ZOMER-27",
            "kind": DiscountKind.PERCENT,
            "value": "10",
            "valid_from": "2027-06-01",
            "valid_until": "2027-08-31",
            "max_uses": "",
            "is_active": "on",
            "description": "Zomeractie",
        }
        data.update(values)
        return {key: value for key, value in data.items() if value is not None}

    def create(self, **values):
        return self.client.post(
            reverse("dashboard:discount_create"), self.data(**values)
        )

    def edit(self, discount, **values):
        return self.client.post(
            reverse("dashboard:discount_edit", args=[discount.pk]), self.data(**values)
        )


class DiscountListTests(DiscountTestCase):
    def test_columns_and_status(self):
        day = timedelta(days=1)
        DiscountCodeFactory(
            code="ACTIEF",
            value=Decimal("15.00"),
            max_uses=10,
            times_used=3,
            description="Voor de nieuwsbrief",
        )
        DiscountCodeFactory(
            code="VAST", kind=DiscountKind.FIXED, value=Decimal("25.00")
        )
        DiscountCodeFactory(code="UIT", is_active=False)
        DiscountCodeFactory(
            code="VERLOPEN",
            valid_from=self.today - 10 * day,
            valid_until=self.today - day,
        )
        DiscountCodeFactory(code="LATER", valid_from=self.today + day)
        DiscountCodeFactory(code="OP", max_uses=2, times_used=2)

        response = self.client.get(reverse("dashboard:discount_list"))

        self.assertContains(response, "6 kortingscodes")
        statuses = {
            row["code"].code: str(row["status"][0]) for row in response.context["rows"]
        }
        self.assertEqual(
            statuses,
            {
                "ACTIEF": "Actief",
                "LATER": "Nog niet geldig",
                "OP": "Op",
                "UIT": "Uitgezet",
                "VAST": "Actief",
                "VERLOPEN": "Verlopen",
            },
        )
        self.assertContains(response, "15%")
        self.assertContains(response, "€ 25,00")
        self.assertContains(response, "Voor de nieuwsbrief")
        self.assertContains(response, '3 <span class="text-muted">/ 10</span>')
        self.assertContains(response, '0 <span class="text-muted">/ onbeperkt</span>')
        self.assertContains(response, reverse("dashboard:discount_create"))

    def test_empty(self):
        response = self.client.get(reverse("dashboard:discount_list"))
        self.assertContains(response, "Nog geen kortingscodes")


class DiscountCreateTests(DiscountTestCase):
    def test_add_a_code_in_upper_case(self):
        response = self.create(code="  zomer-27 ", max_uses="100")

        self.assertRedirects(response, reverse("dashboard:discount_list"))
        code = DiscountCode.objects.get()
        self.assertEqual(code.code, "ZOMER-27")
        self.assertEqual(code.kind, DiscountKind.PERCENT)
        self.assertEqual(code.value, Decimal("10.00"))
        self.assertEqual(code.valid_from, date(2027, 6, 1))
        self.assertEqual(code.valid_until, date(2027, 8, 31))
        self.assertEqual(code.max_uses, 100)
        self.assertEqual(code.times_used, 0)
        self.assertTrue(code.is_active)
        self.assertEqual(code.created_by, self.staff)

    def test_empty_maximum_is_unlimited(self):
        self.create()
        self.assertIsNone(DiscountCode.objects.get().max_uses)

    def test_a_code_exists_once_whatever_the_case(self):
        DiscountCodeFactory(code="ZOMER-27")
        response = self.create(code="Zomer-27")
        self.assertEqual(
            response.context["form"].errors["code"], ["Deze code bestaat al."]
        )
        self.assertEqual(DiscountCode.objects.count(), 1)

    def test_invalid_values(self):
        for values, field in (
            ({"code": "AB"}, "code"),
            ({"code": "ZOMER 27"}, "code"),
            ({"code": "ZOMER_27"}, "code"),
            ({"code": "X" * 41}, "code"),
            ({"value": "0"}, "value"),
            ({"value": "-5"}, "value"),
            ({"value": "100.01"}, "value"),
            ({"kind": "gratis"}, "kind"),
            ({"valid_until": "2027-05-31"}, "valid_until"),
            ({"max_uses": "0"}, "max_uses"),
            ({"max_uses": "-1"}, "max_uses"),
            ({"valid_from": ""}, "valid_from"),
        ):
            with self.subTest(values=values):
                response = self.create(**values)
                self.assertEqual(response.status_code, 200)
                self.assertIn(field, response.context["form"].errors)
                self.assertContains(response, f'aria-describedby="id_{field}_errors"')
        self.assertFalse(DiscountCode.objects.exists())

    def test_a_fixed_amount_may_be_above_100(self):
        self.create(kind=DiscountKind.FIXED, value="150")
        self.assertEqual(DiscountCode.objects.get().value, Decimal("150.00"))

    def test_a_percentage_of_100_is_allowed(self):
        self.create(value="100")
        self.assertEqual(DiscountCode.objects.get().value, Decimal("100.00"))

    def test_kind_has_no_empty_choice(self):
        response = self.client.get(reverse("dashboard:discount_create"))
        self.assertEqual(
            [
                value
                for value, _label in response.context["form"].fields["kind"].choices
            ],
            ["percent", "fixed"],
        )


class DiscountUpdateTests(DiscountTestCase):
    def test_change_an_unused_code(self):
        code = DiscountCodeFactory(code="OUD", value=Decimal("5.00"))

        response = self.edit(
            code, code="nieuw", kind=DiscountKind.FIXED, value="20", max_uses="3"
        )

        self.assertRedirects(response, reverse("dashboard:discount_list"))
        code.refresh_from_db()
        self.assertEqual(
            (code.code, code.kind, code.value, code.max_uses),
            ("NIEUW", DiscountKind.FIXED, Decimal("20.00"), 3),
        )

    def test_used_code_has_fixed_code_kind_and_value(self):
        code = DiscountCodeFactory(code="VAST", value=Decimal("10.00"), times_used=2)

        response = self.client.get(reverse("dashboard:discount_edit", args=[code.pk]))

        form = response.context["form"]
        self.assertTrue(form.is_locked)
        for name in ("code", "kind", "value"):
            self.assertTrue(form.fields[name].disabled)
            self.assertIn("cursor-not-allowed", form.fields[name].widget.attrs["class"])
        self.assertNotIn(
            "cursor-not-allowed", form.fields["max_uses"].widget.attrs["class"]
        )
        self.assertContains(response, "Deze code is al gebruikt.")
        self.assertContains(response, "2 keer gebruikt, geen maximum.")

        # The browser does not send disabled fields; the rest can change.
        response = self.edit(
            code,
            code=None,
            kind=None,
            value=None,
            valid_until="2027-12-31",
            max_uses="5",
            is_active="",
        )
        self.assertRedirects(response, reverse("dashboard:discount_list"))
        code.refresh_from_db()
        self.assertEqual(
            (code.code, code.kind, code.value),
            ("VAST", DiscountKind.PERCENT, Decimal("10.00")),
        )
        self.assertEqual(code.valid_until, date(2027, 12, 31))
        self.assertEqual(code.max_uses, 5)
        self.assertFalse(code.is_active)
        self.assertEqual(code.times_used, 2)

    def test_changing_a_fixed_field_of_a_used_code_is_refused(self):
        code = DiscountCodeFactory(code="VAST", value=Decimal("10.00"), times_used=1)

        for values in ({"value": "50"}, {"code": "ANDERS"}, {"kind": "fixed"}):
            with self.subTest(values=values):
                response = self.edit(code, **{"code": "VAST", "value": "10", **values})
                self.assertEqual(response.status_code, 200)
                self.assertIn(
                    "Code, soort en waarde liggen daarom vast",
                    response.context["form"].non_field_errors()[0],
                )
        code.refresh_from_db()
        self.assertEqual((code.code, code.value), ("VAST", Decimal("10.00")))

    def test_the_same_values_of_a_used_code_are_fine(self):
        code = DiscountCodeFactory(code="VAST", value=Decimal("10.00"), times_used=1)
        response = self.edit(code, code="vast", kind="percent", value="10.00")
        self.assertRedirects(response, reverse("dashboard:discount_list"))

    def test_a_released_use_still_locks_the_code(self):
        # The order expired: times_used went back to 0, but it was used.
        code = DiscountCodeFactory(code="TERUG")
        DiscountRedemption.objects.create(
            code=code,
            order=OrderFactory(),
            amount=Decimal("10.00"),
            released_at=timezone.now(),
        )
        response = self.client.get(reverse("dashboard:discount_edit", args=[code.pk]))
        self.assertTrue(response.context["form"].is_locked)
        self.assertContains(response, "verlopen, telt niet mee")

    def test_maximum_not_below_the_uses(self):
        code = DiscountCodeFactory(max_uses=10, times_used=4)

        response = self.edit(code, code=None, kind=None, value=None, max_uses="3")
        self.assertEqual(
            response.context["form"].errors["max_uses"],
            ["De code is al 4 keer gebruikt; het maximum kan niet lager zijn."],
        )

        response = self.edit(code, code=None, kind=None, value=None, max_uses="4")
        self.assertRedirects(response, reverse("dashboard:discount_list"))
        code.refresh_from_db()
        self.assertEqual(code.max_uses, 4)

    def test_a_use_while_saving_is_not_overwritten(self):
        """
        A guest uses the code between loading and saving the form: the counter is
        not overwritten with the old value, and the checks use the new value.
        """
        code = DiscountCodeFactory(max_uses=10, times_used=4)
        real_clean = DiscountCodeForm.clean

        def guest_books_meanwhile(form):
            DiscountCode.objects.filter(pk=code.pk).update(
                times_used=F("times_used") + 1
            )
            return real_clean(form)

        with mock.patch.object(
            DiscountCodeForm, "clean", autospec=True, side_effect=guest_books_meanwhile
        ):
            response = self.edit(
                code,
                code=None,
                kind=None,
                value=None,
                max_uses="10",
                description="Nieuw",
            )
        self.assertRedirects(response, reverse("dashboard:discount_list"))
        code.refresh_from_db()
        self.assertEqual(code.times_used, 5)
        self.assertEqual(code.description, "Nieuw")

        with mock.patch.object(
            DiscountCodeForm, "clean", autospec=True, side_effect=guest_books_meanwhile
        ):
            response = self.edit(code, code=None, kind=None, value=None, max_uses="5")
        # 6 uses by now: a maximum of 5 is refused instead of breaking the counter.
        self.assertEqual(
            response.context["form"].errors["max_uses"],
            ["De code is al 6 keer gebruikt; het maximum kan niet lager zijn."],
        )
        code.refresh_from_db()
        self.assertEqual((code.times_used, code.max_uses), (6, 10))

    def test_first_use_while_saving_keeps_code_and_value(self):
        code = DiscountCodeFactory(code="NIEUW", value=Decimal("10.00"))
        real_clean = DiscountCodeForm.clean

        def first_use_meanwhile(form):
            DiscountCode.objects.filter(pk=code.pk).update(times_used=1)
            return real_clean(form)

        with mock.patch.object(
            DiscountCodeForm, "clean", autospec=True, side_effect=first_use_meanwhile
        ):
            response = self.edit(code, code="NIEUW", value="50")

        self.assertEqual(response.status_code, 200)
        # The service checks again on the locked row.
        self.assertIn(
            "inmiddels gebruikt", response.context["form"].non_field_errors()[0]
        )
        code.refresh_from_db()
        self.assertEqual(code.value, Decimal("10.00"))

    def test_unknown_code_is_404(self):
        response = self.client.get(reverse("dashboard:discount_edit", args=[999]))
        self.assertEqual(response.status_code, 404)
