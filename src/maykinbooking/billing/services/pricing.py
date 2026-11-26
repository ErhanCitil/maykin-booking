"""
Prices of nights and discounts: the weekend and season surcharges, the discount
for a long stay and discount codes.

Money is ``Decimal``, rounded half up to cents (:func:`money`). The surcharges
of a night are multiplied and the result is rounded once:
``base × (1 + weekend%) × (1 + season%)``. The weekend is the Friday and the
Saturday night.

This module never imports the booking service (which imports this one); the
quote of a stay is ``bookings.services.booking.quote``.
"""
import logging
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional, Tuple

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from django.utils.formats import number_format
from django.utils.translation import gettext, gettext_lazy as _

from maykinbooking.bookings.exceptions import BookingError

from ..models import (
    DISCOUNT_CODE_PATTERN,
    DiscountCode,
    DiscountKind,
    DiscountRedemption,
    PricingSettings,
    SeasonPeriod,
    normalize_discount_code,
)

logger = logging.getLogger(__name__)

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
HUNDRED = Decimal("100")
# date.weekday() of the Friday and Saturday night.
WEEKEND_NIGHTS = (4, 5)

DISCOUNT_CODE = re.compile(DISCOUNT_CODE_PATTERN)


def money(value):
    """``value`` rounded half up to cents."""
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def format_percent(value):
    """``Decimal("10.00")`` as ``"10"``, ``Decimal("12.50")`` as ``"12,5"`` (nl)."""
    value = Decimal(value)
    if value == value.to_integral_value():
        return str(int(value))
    return number_format(value.normalize())


# Labels of surcharges and discounts: one place for the checkout, "Mijn
# boeking", the mails, the PDFs and the invoices. In the active language.


def surcharge_labels(night):
    """
    The surcharges of a night, e.g. ``["weekend +20%", "Hoogseizoen +50%"]``.
    ``night``: an ``OrderNight`` or a :class:`NightPrice` (the same fields).
    """
    labels = []
    if night.weekend_percent:
        labels.append(
            gettext("weekend +%(percent)s%%")
            % {"percent": format_percent(night.weekend_percent)}
        )
    if night.season_percent:
        labels.append(
            gettext("%(season)s +%(percent)s%%")
            % {
                "season": night.season_name or gettext("seizoen"),
                "percent": format_percent(night.season_percent),
            }
        )
    return labels


def night_lines(nights):
    """
    The nights of a stay for a price breakdown: ``[{"date", "price",
    "base_price", "surcharges"}]``. ``nights``: ``OrderNight``s or
    :class:`NightPrice`s.
    """
    return [
        {
            "date": night.date,
            "price": night.price,
            "base_price": night.base_price,
            "surcharges": surcharge_labels(night),
        }
        for night in nights
    ]


def discount_lines(priced):
    """
    The discounts as ``[{"label", "amount"}]``, the amount positive (show it
    with a minus). ``priced``: an ``Order`` or a ``Quote``; both have
    ``length_discount_percent``, ``length_discount``, ``discount_code`` and
    ``code_discount``.
    """
    lines = []
    if priced.length_discount:
        lines.append(
            {
                "label": gettext("Korting lang verblijf (%(percent)s%%)")
                % {"percent": format_percent(priced.length_discount_percent)},
                "amount": priced.length_discount,
            }
        )
    if priced.code_discount:
        lines.append(
            {
                "label": gettext("Kortingscode %(code)s")
                % {"code": priced.discount_code},
                "amount": priced.code_discount,
            }
        )
    return lines


@dataclass(frozen=True)
class Season:
    name: str
    start_date: object
    end_date: object  # inclusive
    percent: Decimal

    def covers(self, day):
        return self.start_date <= day <= self.end_date


@dataclass(frozen=True)
class PricingRules:
    weekend_percent: Decimal = ZERO
    long_stay_min_nights: int = 7
    long_stay_percent: Decimal = ZERO
    seasons: Tuple[Season, ...] = ()

    @classmethod
    def load(cls, start, end):
        """The current rules for the nights of ``[start, end)``: two queries."""
        settings_row = PricingSettings.load()
        seasons = SeasonPeriod.objects.filter(
            start_date__lt=end, end_date__gte=start
        ).order_by("start_date", "pk")
        return cls(
            weekend_percent=settings_row.weekend_percent,
            long_stay_min_nights=settings_row.long_stay_min_nights,
            long_stay_percent=settings_row.long_stay_percent,
            seasons=tuple(
                Season(
                    name=season.name,
                    start_date=season.start_date,
                    end_date=season.end_date,
                    percent=season.percent,
                )
                for season in seasons
            ),
        )

    def season_on(self, day) -> Optional[Season]:
        for season in self.seasons:
            if season.covers(day):
                return season
        return None


@dataclass(frozen=True)
class NightPrice:
    """The price of one night and how it was made; stored on ``OrderNight``."""

    date: object
    base_price: Decimal
    weekend_percent: Decimal
    season_percent: Decimal
    season_name: str
    price: Decimal


def night_price(base, day, rules) -> NightPrice:
    """The price of the night of ``day`` for a room of ``base`` per night."""
    base = Decimal(str(base))
    weekend = rules.weekend_percent if day.weekday() in WEEKEND_NIGHTS else ZERO
    season = rules.season_on(day)
    season_percent = season.percent if season else ZERO
    price = money(base * (1 + weekend / HUNDRED) * (1 + season_percent / HUNDRED))
    return NightPrice(
        date=day,
        base_price=base,
        weekend_percent=Decimal(weekend),
        season_percent=Decimal(season_percent),
        season_name=season.name if season else "",
        price=price,
    )


def length_discount(subtotal, nights, rules):
    """``(percent, amount)`` of the discount for a long stay, or ``(0, 0)``."""
    if rules.long_stay_percent > 0 and nights >= rules.long_stay_min_nights:
        return Decimal(rules.long_stay_percent), money(
            subtotal * rules.long_stay_percent / HUNDRED
        )
    return ZERO, ZERO


def discount_amount(code, amount):
    """
    What ``code`` takes off ``amount`` (the subtotal minus the discount for a
    long stay): a percentage of it, or a fixed amount of at most all of it.
    """
    if amount <= 0:
        return ZERO
    if code.kind == DiscountKind.PERCENT:
        discount = money(amount * code.value / HUNDRED)
    else:
        discount = money(code.value)
    return min(discount, amount)


class InvalidDiscountCode(BookingError):
    """
    The discount code cannot be used. The message is the same for every reason,
    so guests cannot find out which codes exist; ``reason`` is for the logs:
    ``format``, ``unknown``, ``inactive``, ``not_yet_valid``, ``expired`` or
    ``used_up``.
    """

    def __init__(self, reason):
        super().__init__(_("Deze kortingscode is niet geldig."))
        self.reason = reason


def check_discount_code(code, *, today=None) -> DiscountCode:
    """
    The ``DiscountCode`` for ``code`` (as typed) if it can be used today.
    Read-only; raises ``InvalidDiscountCode``.
    """
    normalized = normalize_discount_code(code)
    if not DISCOUNT_CODE.fullmatch(normalized):
        raise InvalidDiscountCode("format")
    today = today or timezone.localdate()
    discount = DiscountCode.objects.filter(code=normalized).first()
    if discount is None:
        raise InvalidDiscountCode("unknown")
    if not discount.is_active:
        raise InvalidDiscountCode("inactive")
    if today < discount.valid_from:
        raise InvalidDiscountCode("not_yet_valid")
    if today > discount.valid_until:
        raise InvalidDiscountCode("expired")
    if discount.max_uses is not None and discount.times_used >= discount.max_uses:
        raise InvalidDiscountCode("used_up")
    return discount


def redeem_discount_code(code, order, amount, *, today=None) -> DiscountRedemption:
    """
    Count one use of ``code`` for ``order`` (giving ``amount`` off). Call it in
    the transaction that creates the order.

    The counter goes up in one ``UPDATE`` that checks the conditions again, so
    of two bookings that want the last use, one gets ``InvalidDiscountCode``
    (``used_up``).
    """
    today = today or timezone.localdate()
    discount = check_discount_code(code, today=today)
    updated = (
        DiscountCode.objects.filter(
            pk=discount.pk,
            is_active=True,
            valid_from__lte=today,
            valid_until__gte=today,
        )
        .filter(Q(max_uses__isnull=True) | Q(times_used__lt=F("max_uses")))
        .update(times_used=F("times_used") + 1)
    )
    if updated != 1:
        raise InvalidDiscountCode("used_up")
    return DiscountRedemption.objects.create(code=discount, order=order, amount=amount)


def release_discount_code(order) -> bool:
    """
    Give the use of a discount code by ``order`` back (the order expired). The
    caller has locked the order. Returns ``False`` if there was nothing to give
    back. A cancelled order is never released: it keeps counting.
    """
    redemption = (
        DiscountRedemption.objects.select_for_update()
        .filter(order=order, released_at__isnull=True)
        .first()
    )
    if redemption is None:
        return False
    redemption.released_at = timezone.now()
    redemption.save(update_fields=["released_at"])
    DiscountCode.objects.filter(pk=redemption.code_id, times_used__gt=0).update(
        times_used=F("times_used") - 1
    )
    return True


def save_season(season):
    """
    Save a ``SeasonPeriod`` if it does not overlap another one. The price rules
    row is locked meanwhile, so two staff members cannot add overlapping
    seasons at the same time. Raises ``ValidationError`` (code ``overlap`` or
    ``end_before_start``).
    """
    if season.end_date < season.start_date:
        raise ValidationError(
            _("De einddatum mag niet voor de begindatum liggen."),
            code="end_before_start",
        )
    with transaction.atomic():
        PricingSettings.load()
        PricingSettings.objects.select_for_update().get(pk=1)
        overlapping = (
            SeasonPeriod.objects.filter(
                start_date__lte=season.end_date, end_date__gte=season.start_date
            )
            .exclude(pk=season.pk)
            .first()
        )
        if overlapping is not None:
            raise ValidationError(
                _("Deze periode overlapt met het seizoen “%(name)s”."),
                code="overlap",
                params={"name": overlapping.name},
            )
        season.save()
    return season
