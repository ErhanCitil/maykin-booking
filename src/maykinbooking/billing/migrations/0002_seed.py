"""
The VAT rates for accommodation in the Netherlands (9% up to 2025, 21% from
1 January 2026) and the price rules row with every percentage at 0.
"""
from datetime import date
from decimal import Decimal

from django.db import migrations

VAT_RATES = [
    ("NL", Decimal("9.00"), date(2019, 1, 1), "Logies, verlaagd tarief"),
    ("NL", Decimal("21.00"), date(2026, 1, 1), "Logies, algemeen tarief vanaf 2026"),
]


def seed(apps, schema_editor):
    VatRate = apps.get_model("billing", "VatRate")
    PricingSettings = apps.get_model("billing", "PricingSettings")
    for country, rate, valid_from, note in VAT_RATES:
        VatRate.objects.get_or_create(
            country=country,
            valid_from=valid_from,
            defaults={"rate": rate, "note": note},
        )
    PricingSettings.objects.get_or_create(
        pk=1,
        defaults={
            "weekend_percent": Decimal("0"),
            "long_stay_min_nights": 7,
            "long_stay_percent": Decimal("0"),
        },
    )


def unseed(apps, schema_editor):
    VatRate = apps.get_model("billing", "VatRate")
    PricingSettings = apps.get_model("billing", "PricingSettings")
    for country, rate, valid_from, _note in VAT_RATES:
        VatRate.objects.filter(
            country=country, valid_from=valid_from, rate=rate
        ).delete()
    PricingSettings.objects.filter(pk=1).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
