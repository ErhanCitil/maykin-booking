"""
The price rules in the dashboard: the weekend surcharge, the discount for a long
stay and the high seasons (US6.1).
"""
from datetime import date
from decimal import Decimal

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from maykinbooking.accounts.tests.factories import ManagerUserFactory
from maykinbooking.billing.models import PricingSettings, SeasonPeriod
from maykinbooking.billing.tests.factories import SeasonPeriodFactory


class PricingTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = ManagerUserFactory(username="beheerder")

    def setUp(self):
        self.client.force_login(self.staff)

    def messages(self, response):
        return [str(message) for message in get_messages(response.wsgi_request)]


class PricingSettingsTests(PricingTestCase):
    url = reverse("dashboard:pricing_rules")

    def post(self, **data):
        values = {
            "weekend_percent": "0",
            "long_stay_min_nights": "7",
            "long_stay_percent": "0",
        }
        values.update(data)
        return self.client.post(self.url, values)

    def test_shows_the_current_rules(self):
        PricingSettings.objects.filter(pk=1).update(weekend_percent=Decimal("15.00"))

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["form"]["weekend_percent"].value(), Decimal("15.00")
        )
        self.assertContains(response, "Weekendtoeslag (%)")
        self.assertContains(response, 'name="long_stay_min_nights"')

    def test_saves_the_rules_and_who_changed_them(self):
        response = self.post(
            weekend_percent="20", long_stay_min_nights="7", long_stay_percent="10"
        )

        self.assertRedirects(response, self.url)
        rules = PricingSettings.load()
        self.assertEqual(rules.weekend_percent, Decimal("20.00"))
        self.assertEqual(rules.long_stay_min_nights, 7)
        self.assertEqual(rules.long_stay_percent, Decimal("10.00"))
        self.assertEqual(rules.updated_by, self.staff)
        self.assertIn("De prijsregels zijn opgeslagen.", self.messages(response)[0])

        response = self.client.get(self.url)
        self.assertContains(response, "Laatst gewijzigd door beheerder")

    def test_the_example_follows_the_rules(self):
        # The examples of the requirements: V1 and V2.
        self.post(
            weekend_percent="20", long_stay_min_nights="7", long_stay_percent="10"
        )

        response = self.client.get(self.url)

        example = response.context["example"]
        self.assertEqual(
            [night.price for night in example["nights"]],
            [Decimal("100.00"), Decimal("120.00"), Decimal("120.00")],
        )
        self.assertEqual(example["total"], Decimal("340.00"))
        self.assertEqual(example["long_stay"]["total"], Decimal("630.00"))
        self.assertContains(response, "€ 340,00")
        self.assertContains(response, "min 10% (€ 70,00) = € 630,00")

    def test_the_example_with_a_season(self):
        self.post(weekend_percent="20")
        SeasonPeriodFactory(name="Zomer", percent=Decimal("50.00"))

        response = self.client.get(self.url)

        self.assertEqual(
            response.context["example"]["season"]["price"], Decimal("180.00")
        )
        self.assertContains(response, "“Zomer” (+50%), dan kost die nacht € 180,00")

    def test_without_discount_the_example_says_so(self):
        response = self.client.get(self.url)
        self.assertContains(response, "Er is nu geen korting voor een lang verblijf.")

    def test_limits(self):
        for data, field in (
            ({"weekend_percent": "200.01"}, "weekend_percent"),
            ({"weekend_percent": "-1"}, "weekend_percent"),
            ({"long_stay_min_nights": "1"}, "long_stay_min_nights"),
            ({"long_stay_min_nights": "31"}, "long_stay_min_nights"),
            ({"long_stay_percent": "100.5"}, "long_stay_percent"),
            ({"long_stay_percent": "abc"}, "long_stay_percent"),
            ({"weekend_percent": "12.345"}, "weekend_percent"),
        ):
            with self.subTest(data=data):
                response = self.post(**data)
                self.assertEqual(response.status_code, 200)
                self.assertIn(field, response.context["form"].errors)
                # The error is linked to its field for screen readers.
                self.assertContains(response, f'aria-describedby="id_{field}_errors"')
        self.assertEqual(PricingSettings.load().weekend_percent, Decimal("0.00"))

    def test_the_highest_values_are_allowed(self):
        self.post(
            weekend_percent="200", long_stay_min_nights="30", long_stay_percent="100"
        )
        rules = PricingSettings.load()
        self.assertEqual(
            (
                rules.weekend_percent,
                rules.long_stay_min_nights,
                rules.long_stay_percent,
            ),
            (Decimal("200.00"), 30, Decimal("100.00")),
        )


class SeasonTests(PricingTestCase):
    def season_data(self, **data):
        values = {
            "name": "Zomer 2027",
            "start_date": "2027-07-01",
            "end_date": "2027-08-31",
            "percent": "25",
        }
        values.update(data)
        return values

    def create(self, **data):
        return self.client.post(
            reverse("dashboard:season_create"), self.season_data(**data)
        )

    def test_add_a_season(self):
        response = self.client.get(reverse("dashboard:season_create"))
        self.assertContains(response, "Nieuw seizoen")

        response = self.create()

        self.assertRedirects(response, reverse("dashboard:pricing_rules"))
        season = SeasonPeriod.objects.get()
        self.assertEqual(season.name, "Zomer 2027")
        self.assertEqual(season.start_date, date(2027, 7, 1))
        self.assertEqual(season.end_date, date(2027, 8, 31))
        self.assertEqual(season.percent, Decimal("25.00"))
        self.assertIn("“Zomer 2027” is toegevoegd", self.messages(response)[0])

        response = self.client.get(reverse("dashboard:pricing_rules"))
        self.assertContains(response, "Zomer 2027")
        self.assertContains(response, "+25%")
        self.assertContains(
            response, reverse("dashboard:season_edit", args=[season.pk])
        )

    def test_overlap_is_shown_with_the_form(self):
        SeasonPeriodFactory(
            name="Kerst", start_date=date(2027, 12, 20), end_date=date(2028, 1, 3)
        )

        for start, end in (
            ("2027-12-01", "2027-12-20"),  # ends on the first day
            ("2028-01-03", "2028-01-10"),  # starts on the last day (inclusive)
            ("2027-12-24", "2027-12-26"),  # inside
            ("2027-12-01", "2028-02-01"),  # around
        ):
            with self.subTest(start=start, end=end):
                response = self.create(name="Winter", start_date=start, end_date=end)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.context["form"].non_field_errors(),
                    ["Deze periode overlapt met het seizoen “Kerst”."],
                )
                self.assertContains(response, 'role="alert"')
                self.assertContains(response, "Deze periode overlapt met het seizoen")
        self.assertEqual(SeasonPeriod.objects.count(), 1)

    def test_adjacent_seasons_are_fine(self):
        SeasonPeriodFactory(
            name="Kerst", start_date=date(2027, 12, 20), end_date=date(2028, 1, 3)
        )
        self.create(name="Voor", start_date="2027-12-01", end_date="2027-12-19")
        self.create(name="Na", start_date="2028-01-04", end_date="2028-01-31")
        self.assertEqual(SeasonPeriod.objects.count(), 3)

    def test_end_before_start(self):
        response = self.create(start_date="2027-08-01", end_date="2027-07-31")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["form"].errors["end_date"],
            ["De einddatum mag niet voor de begindatum liggen."],
        )
        self.assertFalse(SeasonPeriod.objects.exists())

    def test_one_day_season(self):
        self.create(start_date="2027-04-27", end_date="2027-04-27")
        self.assertEqual(SeasonPeriod.objects.get().start_date, date(2027, 4, 27))

    def test_percent_limits_and_required_fields(self):
        response = self.create(percent="201")
        self.assertIn("percent", response.context["form"].errors)
        response = self.create(name="", start_date="")
        self.assertIn("name", response.context["form"].errors)
        self.assertIn("start_date", response.context["form"].errors)
        self.assertFalse(SeasonPeriod.objects.exists())

    def test_edit_a_season_without_overlapping_itself(self):
        season = SeasonPeriodFactory(
            name="Zomer", start_date=date(2027, 7, 1), end_date=date(2027, 8, 31)
        )
        url = reverse("dashboard:season_edit", args=[season.pk])
        self.assertContains(self.client.get(url), 'value="2027-07-01"')

        response = self.client.post(
            url, self.season_data(name="Lange zomer", end_date="2027-09-15")
        )

        self.assertRedirects(response, reverse("dashboard:pricing_rules"))
        season.refresh_from_db()
        self.assertEqual(season.name, "Lange zomer")
        self.assertEqual(season.end_date, date(2027, 9, 15))

    def test_edit_into_another_season(self):
        SeasonPeriodFactory(
            name="Herfst", start_date=date(2027, 10, 1), end_date=date(2027, 10, 31)
        )
        season = SeasonPeriodFactory(
            name="Zomer", start_date=date(2027, 7, 1), end_date=date(2027, 8, 31)
        )

        response = self.client.post(
            reverse("dashboard:season_edit", args=[season.pk]),
            self.season_data(end_date="2027-10-01"),
        )

        self.assertContains(response, "Deze periode overlapt met het seizoen “Herfst”.")
        season.refresh_from_db()
        self.assertEqual(season.end_date, date(2027, 8, 31))

    def test_delete_with_confirmation(self):
        season = SeasonPeriodFactory(name="Zomer")
        delete_url = reverse("dashboard:season_delete", args=[season.pk])
        response = self.client.get(reverse("dashboard:pricing_rules"))
        self.assertContains(response, f'action="{delete_url}"')
        self.assertContains(response, "“Zomer” verwijderen?")

        # Only POST deletes.
        self.assertEqual(self.client.get(delete_url).status_code, 405)
        self.assertTrue(SeasonPeriod.objects.exists())

        response = self.client.post(delete_url)
        self.assertRedirects(response, reverse("dashboard:pricing_rules"))
        self.assertFalse(SeasonPeriod.objects.exists())
        self.assertIn("“Zomer” is verwijderd", self.messages(response)[0])

    def test_unknown_season_is_404(self):
        self.assertEqual(
            self.client.get(reverse("dashboard:season_edit", args=[999])).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                reverse("dashboard:season_delete", args=[999])
            ).status_code,
            404,
        )
