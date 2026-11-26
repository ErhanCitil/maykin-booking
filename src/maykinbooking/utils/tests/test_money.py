"""The ``euro`` filters: one notation for amounts."""
from decimal import Decimal

from django.template import Context, Template
from django.test import SimpleTestCase
from django.utils import translation

from ..templatetags.money import format_euro


def render(template, **context):
    return Template("{% load money %}" + template).render(Context(context))


class EuroTests(SimpleTestCase):
    def test_amounts(self):
        for value, expected in (
            (Decimal("280.99"), "€ 280,99"),
            (Decimal("-280.99"), "− € 280,99"),
            (Decimal("0"), "€ 0,00"),
            (Decimal("1234567.5"), "€ 1.234.567,50"),
            (Decimal("-1234.5"), "− € 1.234,50"),
            (12, "€ 12,00"),
            (12.5, "€ 12,50"),
            ("99.99", "€ 99,99"),
        ):
            with self.subTest(value=value):
                self.assertEqual(format_euro(value), expected)

    def test_half_up_to_cents(self):
        self.assertEqual(format_euro(Decimal("11.165")), "€ 11,17")
        self.assertEqual(format_euro(Decimal("-11.165")), "− € 11,17")
        self.assertEqual(format_euro(Decimal("0.004")), "€ 0,00")

    def test_negative_zero_is_zero(self):
        for value in (Decimal("-0.00"), Decimal("-0"), Decimal("-0.004"), -0.0):
            with self.subTest(value=value):
                self.assertEqual(format_euro(value), "€ 0,00")

    def test_no_amount(self):
        for value in (None, "", "abc", object()):
            with self.subTest(value=value):
                self.assertEqual(format_euro(value), "")

    def test_always_dutch(self):
        with translation.override("en"):
            self.assertEqual(format_euro(Decimal("-1234.5")), "− € 1.234,50")

    def test_filters_in_a_template(self):
        self.assertEqual(
            render("{{ amount|euro }}", amount=Decimal("-242.00")), "− € 242,00"
        )
        self.assertEqual(
            render("{{ amount|euro_minus }}", amount=Decimal("24.20")),
            "− € 24,20",
        )
        self.assertEqual(
            render("{{ amount|euro_minus }}", amount=Decimal("-24.20")),
            "− € 24,20",
        )
        self.assertEqual(render("{{ amount|euro_minus }}", amount=0), "€ 0,00")
        self.assertEqual(render("{{ amount|euro }}", amount=None), "")
