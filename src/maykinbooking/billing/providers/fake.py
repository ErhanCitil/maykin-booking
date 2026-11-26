"""
A fake payment provider for development and CI, with the same flow as Mollie.

Its state lives in the database (``FakeProviderPayment``), so a payment survives
a restart of the development server. The guest pays on our own page
(``billing:fake_checkout``) with the buttons "betaald", "mislukt" and
"geannuleerd", and is sent back to the return URL. Refunds are refunded at once.

Never in production: ``conf/production.py`` refuses ``PAYMENT_FAKE_PROVIDER``.
"""
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.urls import reverse

from ..models import (
    FakeProviderPayment,
    FakeProviderRefund,
    PaymentState,
    ProviderName,
    RefundState,
)
from .base import PaymentProvider, ProviderError, ProviderPayment, ProviderRefund

# Ids are derived from the idempotency key, so the same request gives the same
# payment or refund, like Mollie's Idempotency-Key.
ID_NAMESPACE = uuid.UUID("6f1d3c52-2b7e-4f3c-9d0a-8a4f0e1b7c21")


def _fake_id(prefix, idempotency_key):
    return f"{prefix}{uuid.uuid5(ID_NAMESPACE, str(idempotency_key)).hex[:24]}"


def checkout_url(payment_id):
    return settings.SITE_URL.rstrip("/") + reverse(
        "billing:fake_checkout", args=[payment_id]
    )


def _payment(fake):
    return ProviderPayment(
        id=fake.id,
        status=fake.status,
        amount=fake.amount,
        currency=fake.currency,
        checkout_url=checkout_url(fake.id) if fake.status == PaymentState.OPEN else "",
        method=fake.method,
        paid_at=fake.paid_at,
        metadata=dict(fake.metadata),
    )


def _refund(fake):
    return ProviderRefund(
        id=fake.id,
        status=fake.status,
        amount=fake.amount,
        currency=fake.currency,
        metadata=dict(fake.metadata),
    )


class FakeProvider(PaymentProvider):
    name = ProviderName.FAKE

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
    ):
        fake, _created = FakeProviderPayment.objects.get_or_create(
            id=_fake_id("fake_tr_", idempotency_key),
            defaults={
                "amount": Decimal(amount),
                "currency": currency,
                "description": description[:255],
                "redirect_url": redirect_url,
                "metadata": metadata,
            },
        )
        return _payment(fake)

    def _get(self, payment_id):
        try:
            return FakeProviderPayment.objects.get(pk=payment_id)
        except FakeProviderPayment.DoesNotExist:
            raise ProviderError(
                f"Fake payment {payment_id!r} does not exist.",
                retryable=False,
                status_code=404,
            )

    def get_payment(self, payment_id):
        return _payment(self._get(payment_id))

    def create_refund(
        self, payment_id, *, amount, currency, description, metadata, idempotency_key
    ):
        refund_id = _fake_id("fake_re_", idempotency_key)
        with transaction.atomic():
            existing = FakeProviderRefund.objects.filter(pk=refund_id).first()
            if existing is not None:
                return _refund(existing)
            fake = self._get(payment_id)
            if fake.status != PaymentState.PAID:
                raise ProviderError(
                    "Only paid payments can be refunded.",
                    retryable=False,
                    status_code=422,
                )
            refunded = sum(
                (
                    refund.amount
                    for refund in fake.refunds.exclude(status=RefundState.FAILED)
                ),
                Decimal("0.00"),
            )
            if Decimal(amount) > fake.amount - refunded:
                raise ProviderError(
                    "The amount is higher than what is left to refund.",
                    retryable=False,
                    status_code=422,
                )
            refund = FakeProviderRefund.objects.create(
                id=refund_id,
                payment=fake,
                amount=Decimal(amount),
                currency=currency,
                description=description[:255],
                status=RefundState.REFUNDED,
                metadata=metadata,
            )
        return _refund(refund)

    def list_refunds(self, payment_id):
        fake = self._get(payment_id)
        return [_refund(refund) for refund in fake.refunds.order_by("created_at")]
