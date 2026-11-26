"""
Give the existing nights a base price: the price they were booked for. They had
no surcharges, so the base price is the price.

Migration 0016 makes ``base_price`` NOT NULL. It is a separate migration because
PostgreSQL does not allow ALTER TABLE on a table with pending trigger events in
the same transaction.
"""
from django.db import migrations
from django.db.models import F


def backfill(apps, schema_editor):
    OrderNight = apps.get_model("base", "OrderNight")
    OrderNight.objects.filter(base_price=None).update(base_price=F("price"))


class Migration(migrations.Migration):
    dependencies = [
        ("base", "0014_phase2_pricing"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
