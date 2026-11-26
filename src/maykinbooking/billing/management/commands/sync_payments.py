from django.core.management.base import BaseCommand

from maykinbooking.billing.services.payments import sync_open_payments, sync_refunds


class Command(BaseCommand):
    help = (
        "Vraagt de betaalprovider naar de status van open betalingen (ouder dan "
        "2 minuten) en verwerkt die, en verstuurt terugbetalingen die nog niet "
        "gelukt zijn opnieuw. Draai dit bijvoorbeeld elke minuut via cron, naast "
        "expire_pending_orders."
    )

    def handle(self, *args, **options):
        payments = sync_open_payments()
        refunds = sync_refunds()
        if options["verbosity"] >= 1:
            self.stdout.write(
                f"{payments} betaling(en) en {refunds} terugbetaling(en) verwerkt."
            )
