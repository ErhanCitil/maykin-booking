from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class BillingConfig(AppConfig):
    """
    Money: price rules, discount codes, online payments, refunds and invoices.

    Foreign keys only point from billing to ``base``, never the other way round.
    """

    name = "maykinbooking.billing"
    verbose_name = _("Geld")

    def ready(self):
        from . import checks  # noqa: F401 (registers the system checks)
