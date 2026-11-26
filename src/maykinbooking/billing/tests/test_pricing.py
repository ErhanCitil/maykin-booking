"""
The price rules and the calculation examples of the design (section 2).

1 March 2027 is a Monday, so: Thursday 4, Friday 5, Saturday 6, Sunday 7 March.
"""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from maykinbooking.base.tests.factories import RoomFactory
from maykinbooking.bookings.services.booking import quote

from ..models import DiscountKind, PricingSettings, SeasonPeriod
from ..services.invoicing import split_vat, vat_rate_for
from ..services.pricing import (
    PricingRules,
    Season,
    format_percent,
    length_discount,
    money,
    night_price,
    save_season,
)
from .factories import DiscountCodeFactory, SeasonPeriodFactory

MONDAY = date(2027, 3, 1)
THURSDAY = date(2027, 3, 4)
FRIDAY = date(2027, 3, 5)
SATURDAY = date(2027, 3, 6)
SUNDAY = date(2027, 3, 7)
NEXT_MONDAY = date(2027, 3, 8)


def room(price):
    return SimpleNamespace(price=Decimal(price))


def rules(weekend="0", season=None, long_stay="0", min_nights=7):
    seasons = ()
    if season is not None:
        start, end, percent = season
        seasons = (Season("Hoogseizoen", start, end, Decimal(percent)),)
    return PricingRules(
        weekend_percent=Decimal(weekend),
        long_stay_min_nights=min_nights,
        long_stay_percent=Decimal(long_stay),
        seasons=seasons,
    )


def prices(offer):
    return [night.price for night in offer.nights]


class CalculationExampleTests(SimpleTestCase):
    """The examples without a discount code: no database needed."""

    def test_v1_weekend_surcharge_thursday_to_sunday(self):
        offer = quote(room("100"), THURSDAY, SUNDAY, rules=rules(weekend="20"))
        self.assertEqual(
            prices(offer), [Decimal("100.00"), Decimal("120.00"), Decimal("120.00")]
        )
        self.assertEqual(offer.subtotal, Decimal("340.00"))
        self.assertEqual(offer.total, Decimal("340.00"))

    def test_v2_long_stay_discount(self):
        offer = quote(room("100"), MONDAY, NEXT_MONDAY, rules=rules(long_stay="10"))
        self.assertEqual(len(offer.nights), 7)
        self.assertEqual(offer.subtotal, Decimal("700.00"))
        self.assertEqual(offer.length_discount_percent, Decimal("10"))
        self.assertEqual(offer.length_discount, Decimal("70.00"))
        self.assertEqual(offer.total, Decimal("630.00"))

    def test_friday_to_monday(self):
        offer = quote(room("100"), FRIDAY, NEXT_MONDAY, rules=rules(weekend="20"))
        self.assertEqual(
            prices(offer), [Decimal("120.00"), Decimal("120.00"), Decimal("100.00")]
        )

    def test_saturday_with_weekend_and_season(self):
        night = night_price(
            Decimal("100"),
            SATURDAY,
            rules(weekend="20", season=(MONDAY, NEXT_MONDAY, "50")),
        )
        self.assertEqual(night.price, Decimal("180.00"))
        self.assertEqual(night.base_price, Decimal("100"))
        self.assertEqual(night.weekend_percent, Decimal("20"))
        self.assertEqual(night.season_percent, Decimal("50"))
        self.assertEqual(night.season_name, "Hoogseizoen")

    def test_rounded_half_up_once(self):
        # 10.15 × 1.10 = 11.165
        self.assertEqual(
            night_price(Decimal("10.15"), SATURDAY, rules(weekend="10")).price,
            Decimal("11.17"),
        )
        # 99.99 × 1.15 × 1.10 = 126.48735
        self.assertEqual(
            night_price(
                Decimal("99.99"),
                SATURDAY,
                rules(weekend="15", season=(SATURDAY, SATURDAY, "10")),
            ).price,
            Decimal("126.49"),
        )

    def test_season_end_date_is_inclusive(self):
        july = rules(season=(date(2027, 7, 1), date(2027, 7, 31), "50"))
        offer = quote(room("100"), date(2027, 7, 31), date(2027, 8, 2), rules=july)
        self.assertEqual(prices(offer), [Decimal("150.00"), Decimal("100.00")])
        self.assertEqual(
            [night.season_name for night in offer.nights], ["Hoogseizoen", ""]
        )

    def test_six_nights_get_no_long_stay_discount(self):
        offer = quote(
            room("100"), MONDAY, date(2027, 3, 7), rules=rules(long_stay="10")
        )
        self.assertEqual(len(offer.nights), 6)
        self.assertEqual(offer.length_discount, Decimal("0.00"))
        self.assertEqual(offer.length_discount_percent, Decimal("0.00"))
        self.assertEqual(offer.total, Decimal("600.00"))

    def test_long_stay_discount_is_rounded_to_cents(self):
        self.assertEqual(
            length_discount(Decimal("700.05"), 7, rules(long_stay="10")),
            (Decimal("10"), Decimal("70.01")),
        )

    def test_vat_is_computed_back_from_the_total(self):
        self.assertEqual(
            split_vat(Decimal("340.00"), Decimal("21")),
            (Decimal("280.99"), Decimal("59.01")),
        )
        self.assertEqual(
            split_vat(Decimal("630.00"), Decimal("21")),
            (Decimal("520.66"), Decimal("109.34")),
        )
        self.assertEqual(
            split_vat(Decimal("340.00"), Decimal("9")),
            (Decimal("311.93"), Decimal("28.07")),
        )

    def test_money_rounds_half_up(self):
        self.assertEqual(money(Decimal("0.005")), Decimal("0.01"))
        self.assertEqual(money(Decimal("2.675")), Decimal("2.68"))
        self.assertEqual(money(Decimal("-2.675")), Decimal("-2.68"))

    def test_format_percent(self):
        self.assertEqual(format_percent(Decimal("10.00")), "10")
        self.assertEqual(format_percent(Decimal("12.50")), "12,5")


class QuoteWithDiscountCodeTests(TestCase):
    def test_v2_with_a_code_of_ten_percent(self):
        code = DiscountCodeFactory(kind=DiscountKind.PERCENT, value=Decimal("10"))
        offer = quote(
            room("100"),
            MONDAY,
            NEXT_MONDAY,
            discount_code=code.code.lower(),
            rules=rules(long_stay="10"),
        )
        self.assertEqual(offer.discount, code)
        self.assertEqual(offer.code_discount, Decimal("63.00"))
        self.assertEqual(offer.total, Decimal("567.00"))

    def test_fixed_code_takes_at_most_everything(self):
        code = DiscountCodeFactory(kind=DiscountKind.FIXED, value=Decimal("50"))
        offer = quote(
            room("20"), MONDAY, date(2027, 3, 3), discount_code=code.code, rules=rules()
        )
        self.assertEqual(offer.subtotal, Decimal("40.00"))
        self.assertEqual(offer.code_discount, Decimal("40.00"))
        self.assertEqual(offer.total, Decimal("0.00"))


class PricingRulesTests(TestCase):
    def test_defaults_are_zero(self):
        PricingSettings.objects.all().delete()
        loaded = PricingRules.load(MONDAY, NEXT_MONDAY)
        self.assertEqual(loaded.weekend_percent, Decimal("0"))
        self.assertEqual(loaded.long_stay_percent, Decimal("0"))
        self.assertEqual(loaded.long_stay_min_nights, 7)
        self.assertEqual(PricingSettings.objects.get().pk, 1)

    def test_load_takes_two_queries_and_only_the_seasons_of_the_stay(self):
        PricingSettings.load()
        SeasonPeriodFactory(
            name="Voor", start_date=date(2027, 2, 1), end_date=MONDAY - date.resolution
        )
        during = SeasonPeriodFactory(
            name="Tijdens", start_date=SUNDAY, end_date=date(2027, 3, 20)
        )
        SeasonPeriodFactory(
            name="Na", start_date=NEXT_MONDAY, end_date=date(2027, 4, 1)
        )
        with self.assertNumQueries(2):
            loaded = PricingRules.load(MONDAY, NEXT_MONDAY)
        self.assertEqual([season.name for season in loaded.seasons], [during.name])

    def test_quote_uses_the_current_rules(self):
        settings_row = PricingSettings.load()
        settings_row.weekend_percent = Decimal("20")
        settings_row.save()
        offer = quote(RoomFactory(price=Decimal("100")), THURSDAY, SUNDAY)
        self.assertEqual(offer.total, Decimal("340.00"))

    def test_vat_rate_follows_the_check_in_date(self):
        # Seeded by billing/0002.
        self.assertEqual(vat_rate_for("NL", date(2025, 12, 31)), Decimal("9.00"))
        self.assertEqual(vat_rate_for("NL", date(2026, 1, 1)), Decimal("21.00"))
        self.assertIsNone(vat_rate_for("BE", date(2026, 6, 1)))


class SaveSeasonTests(TestCase):
    def test_overlapping_seasons_are_refused(self):
        existing = SeasonPeriodFactory(
            name="Zomer", start_date=date(2027, 7, 1), end_date=date(2027, 7, 31)
        )
        for start, end in [
            (date(2027, 7, 31), date(2027, 8, 10)),
            (date(2027, 6, 1), date(2027, 7, 1)),
            (date(2027, 7, 10), date(2027, 7, 12)),
        ]:
            with self.subTest(start=start, end=end):
                with self.assertRaises(ValidationError) as error:
                    save_season(
                        SeasonPeriod(
                            name="Nieuw", start_date=start, end_date=end, percent=10
                        )
                    )
                self.assertEqual(error.exception.code, "overlap")
        self.assertEqual(list(SeasonPeriod.objects.all()), [existing])

    def test_adjacent_season_and_changing_itself_are_fine(self):
        summer = save_season(
            SeasonPeriod(
                name="Zomer",
                start_date=date(2027, 7, 1),
                end_date=date(2027, 7, 31),
                percent=Decimal("50"),
            )
        )
        save_season(
            SeasonPeriod(
                name="Nazomer",
                start_date=date(2027, 8, 1),
                end_date=date(2027, 8, 31),
                percent=Decimal("20"),
            )
        )
        summer.end_date = date(2027, 7, 30)
        save_season(summer)
        self.assertEqual(SeasonPeriod.objects.count(), 2)

    def test_end_before_start(self):
        with self.assertRaises(ValidationError) as error:
            save_season(
                SeasonPeriod(
                    name="Fout",
                    start_date=date(2027, 7, 2),
                    end_date=date(2027, 7, 1),
                    percent=10,
                )
            )
        self.assertEqual(error.exception.code, "end_before_start")
