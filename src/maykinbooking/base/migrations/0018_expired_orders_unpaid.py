"""
Orders that expired while waiting for an online payment had the payment status
"open", as if the guest could still pay. They get "unpaid", like the orders
that expire from now on (``expire_order``).

Back: "unpaid" becomes "open" again, before 0017 removes it from the allowed
payment statuses.

A separate migration from 0017: PostgreSQL does not allow ALTER TABLE on a
table with pending trigger events in the same transaction.
"""
from django.db import migrations


def mark_unpaid(apps, schema_editor):
    Order = apps.get_model("base", "Order")
    Order.objects.filter(status="expired", payment_status="open").update(
        payment_status="unpaid"
    )


def mark_open(apps, schema_editor):
    Order = apps.get_model("base", "Order")
    Order.objects.filter(payment_status="unpaid").update(payment_status="open")


class Migration(migrations.Migration):
    dependencies = [
        ("base", "0017_payment_status_unpaid"),
    ]

    operations = [
        migrations.RunPython(mark_unpaid, mark_open),
    ]
