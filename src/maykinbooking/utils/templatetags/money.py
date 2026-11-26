"""
One notation for amounts in euro: ``€ 1.234,56``, and ``− € 280,99`` when
negative (with a real minus sign, U+2212). Always in the Dutch notation, also
when another language is active, so an amount looks the same everywhere.

    {% load money %}
    {{ invoice.total_incl|euro }}       → − € 242,00 (credit note)
    {{ line.amount|euro_minus }}        → − € 24,20 (a discount, stored positive)
"""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django import template
from django.utils import numberformat

register = template.Library()

CENT = Decimal("0.01")
MINUS = "−"


def _amount(value):
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        return None


def format_euro(value):
    """
    ``value`` as ``€ 1.234,56`` or ``− € 1.234,56``; rounded half up to cents.
    A negative zero (``-0.00``, or ``-0.004`` after rounding) is ``€ 0,00``.
    ``None``, ``""`` and values that are no number give ``""``.
    """
    amount = _amount(value)
    if amount is None:
        return ""
    sign = MINUS + " " if amount < 0 else ""
    number = numberformat.format(
        abs(amount),
        decimal_sep=",",
        decimal_pos=2,
        grouping=3,
        thousand_sep=".",
        force_grouping=True,
    )
    return f"{sign}€ {number}"


@register.filter
def euro(value):
    """An amount in euro, negative with a minus: ``− € 280,99``."""
    return format_euro(value)


@register.filter
def euro_minus(value):
    """
    An amount that is taken off (a discount, stored as a positive number):
    always shown with a minus, ``− € 24,20``; zero stays ``€ 0,00``.
    """
    amount = _amount(value)
    if amount is None:
        return ""
    return format_euro(-abs(amount))
