from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class BookingsConfig(AppConfig):
    """Booking logic (services, mails, PDF). The models live in ``base``."""

    name = "maykinbooking.bookings"
    verbose_name = _("Boekingen")
