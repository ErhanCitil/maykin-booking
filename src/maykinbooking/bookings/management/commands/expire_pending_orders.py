from django.core.management.base import BaseCommand

from maykinbooking.bookings.services.booking import expire_stale_pending


class Command(BaseCommand):
    help = (
        "Zet boekingen die op betaling wachten en waarvan de reservering is "
        "verlopen op 'verlopen', zodat de kamers weer vrij zijn. Draai dit "
        "bijvoorbeeld elke minuut via cron."
    )

    def handle(self, *args, **options):
        expired = expire_stale_pending()
        if options["verbosity"] >= 1:
            self.stdout.write(f"{expired} boeking(en) verlopen.")
