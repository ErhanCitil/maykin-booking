"""
The interface of a payment provider (Mollie, or the fake one for development).

Providers know nothing about orders: they create and look up payments and
refunds, and translate every failure into a :class:`ProviderError`.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import List, Optional


class ProviderError(Exception):
    """
    The provider could not be reached, or refused the request.

    ``retryable``: trying again later may work (timeout, 5xx). ``status_code``:
    the HTTP status of the provider's answer, if there was one. The message
    never contains the API key.
    """

    def __init__(self, message, *, retryable=False, status_code=None):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class ProviderNotAvailable(ProviderError):
    """The provider is not configured (no API key, or the fake one is off)."""

    def __init__(self, message):
        super().__init__(message, retryable=False)


@dataclass(frozen=True)
class ProviderPayment:
    id: str
    status: str
    amount: Decimal
    currency: str
    checkout_url: str = ""
    method: str = ""
    paid_at: Optional[datetime] = None
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderRefund:
    id: str
    status: str
    amount: Decimal
    currency: str
    metadata: dict = field(default_factory=dict)


class PaymentProvider(ABC):
    """A payment provider. Every method raises ``ProviderError`` on failure."""

    name: str

    @abstractmethod
    def create_payment(
        self,
        *,
        amount,
        currency,
        description,
        redirect_url,
        webhook_url,
        metadata,
        idempotency_key,
    ) -> ProviderPayment:
        """
        Start a payment; the guest pays on ``checkout_url`` of the result and
        comes back on ``redirect_url``. ``webhook_url`` may be ``None``.
        """

    @abstractmethod
    def get_payment(self, payment_id) -> ProviderPayment:
        """The current state of a payment."""

    @abstractmethod
    def create_refund(
        self, payment_id, *, amount, currency, description, metadata, idempotency_key
    ) -> ProviderRefund:
        """Refund ``amount`` of a paid payment."""

    @abstractmethod
    def list_refunds(self, payment_id) -> List[ProviderRefund]:
        """The refunds of a payment."""
