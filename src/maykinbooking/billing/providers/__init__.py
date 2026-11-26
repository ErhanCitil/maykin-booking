"""
Payment providers: Mollie (with ``MOLLIE_API_KEY``) or, in development and CI,
the fake provider (``PAYMENT_FAKE_PROVIDER``). Without either, online payment
is not offered.
"""
from django.conf import settings

from ..models import ProviderName
from .base import (
    PaymentProvider,
    ProviderError,
    ProviderNotAvailable,
    ProviderPayment,
    ProviderRefund,
)

__all__ = [
    "PaymentProvider",
    "ProviderError",
    "ProviderNotAvailable",
    "ProviderPayment",
    "ProviderRefund",
    "get_provider",
    "online_payment_available",
]


def online_payment_available():
    """Whether guests can pay online: Mollie or the fake provider is set up."""
    return bool(settings.MOLLIE_API_KEY) or bool(settings.PAYMENT_FAKE_PROVIDER)


def get_provider(name=None) -> PaymentProvider:
    """
    The provider called ``name`` (a ``ProviderName``), or with ``None`` the one
    new payments use: Mollie when ``MOLLIE_API_KEY`` is set, otherwise the fake
    provider when ``PAYMENT_FAKE_PROVIDER`` is on.

    Raises ``ProviderNotAvailable`` when that provider is not configured.
    """
    if name is None:
        if settings.MOLLIE_API_KEY:
            name = ProviderName.MOLLIE
        elif settings.PAYMENT_FAKE_PROVIDER:
            name = ProviderName.FAKE
        else:
            raise ProviderNotAvailable("Online payment is not configured.")

    if name == ProviderName.MOLLIE:
        if not settings.MOLLIE_API_KEY:
            raise ProviderNotAvailable("Mollie is not configured (MOLLIE_API_KEY).")
        from .mollie import MollieProvider

        return MollieProvider(settings.MOLLIE_API_KEY)
    if name == ProviderName.FAKE:
        if not settings.PAYMENT_FAKE_PROVIDER:
            raise ProviderNotAvailable("The fake payment provider is turned off.")
        from .fake import FakeProvider

        return FakeProvider()
    raise ProviderNotAvailable(f"Unknown payment provider {name!r}.")
