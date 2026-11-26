import datetime
from decimal import Decimal

from django.utils import timezone

import factory

from maykinbooking.base.models import OrderStatus, PaymentStatus
from maykinbooking.base.tests.factories import OrderFactory

from ..models import (
    DiscountCode,
    DiscountKind,
    Payment,
    PaymentState,
    ProviderName,
    Refund,
    RefundReason,
    RefundState,
    SeasonPeriod,
    VatRate,
)


class DiscountCodeFactory(factory.django.DjangoModelFactory):
    """10% off, valid from a month ago until a year from now, unlimited."""

    class Meta:
        model = DiscountCode

    code = factory.Sequence(lambda n: f"KORTING-{n}")
    kind = DiscountKind.PERCENT
    value = Decimal("10.00")
    valid_from = factory.LazyFunction(
        lambda: timezone.localdate() - datetime.timedelta(days=30)
    )
    valid_until = factory.LazyFunction(
        lambda: timezone.localdate() + datetime.timedelta(days=365)
    )
    max_uses = None
    is_active = True


class SeasonPeriodFactory(factory.django.DjangoModelFactory):
    """Two weeks of high season (+50%), starting a month from now."""

    class Meta:
        model = SeasonPeriod

    name = "Hoogseizoen"
    start_date = factory.LazyFunction(
        lambda: timezone.localdate() + datetime.timedelta(days=30)
    )
    end_date = factory.LazyAttribute(
        lambda season: season.start_date + datetime.timedelta(days=13)
    )
    percent = Decimal("50.00")


class VatRateFactory(factory.django.DjangoModelFactory):
    """A VAT rate; the existing one is reused (the migration seeds NL)."""

    class Meta:
        model = VatRate
        django_get_or_create = ("country", "valid_from")

    country = "NL"
    rate = Decimal("21.00")
    valid_from = datetime.date(2026, 1, 1)


class PaymentFactory(factory.django.DjangoModelFactory):
    """
    An open payment at the fake provider of an order that waits for payment
    (the hold runs 15 more minutes).
    """

    class Meta:
        model = Payment

    order = factory.SubFactory(
        OrderFactory,
        status=OrderStatus.PENDING_PAYMENT,
        payment_status=PaymentStatus.OPEN,
        hold_expires_at=factory.LazyFunction(
            lambda: timezone.now() + datetime.timedelta(minutes=15)
        ),
    )
    provider = ProviderName.FAKE
    provider_id = factory.Sequence(lambda n: f"fake_tr_test{n}")
    status = PaymentState.OPEN
    amount = factory.LazyAttribute(lambda payment: payment.order.total_price)
    currency = "EUR"


class RefundFactory(factory.django.DjangoModelFactory):
    """A requested refund of all of a paid payment of a cancelled order."""

    class Meta:
        model = Refund

    payment = factory.SubFactory(
        PaymentFactory,
        status=PaymentState.PAID,
        order__status=OrderStatus.CANCELLED,
        order__payment_status=PaymentStatus.PAID,
        order__hold_expires_at=None,
    )
    amount = factory.LazyAttribute(lambda refund: refund.payment.amount)
    currency = "EUR"
    reason = RefundReason.CANCELLATION
    status = RefundState.REQUESTED
