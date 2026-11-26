"""
Creating and changing discount codes (dashboard, "Kortingscodes").

The rules, also when a guest uses the code at the same moment:

* the code is stored in upper case, and exists once;
* once a code has been used (``times_used`` > 0, or any redemption, also one
  that was given back), its code, kind and value are fixed: orders and
  invoices keep a snapshot of what it was;
* ``max_uses`` (if set) is at least 1 and never below ``times_used``;
* ``times_used`` is never written here: only ``redeem_discount_code`` and
  ``release_discount_code`` change it, with a single ``UPDATE``.

A change locks the row (``select_for_update``) and checks the rules against
the current ``times_used``, like ``save_season`` does for overlap. Problems
raise ``ValidationError``: a dict per field, or one message (code ``locked``)
when a fixed field was changed.

Locks: only the discount code (it is last in the lock order Room → Order →
Payment/Refund → DiscountCode → InvoiceSequence).
"""
import logging

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils.translation import gettext_lazy as _

from ..models import DiscountCode, DiscountKind, normalize_discount_code

logger = logging.getLogger(__name__)

EDITABLE_FIELDS = (
    "code",
    "kind",
    "value",
    "valid_from",
    "valid_until",
    "max_uses",
    "is_active",
    "description",
)
# Fixed after the first use.
LOCKED_FIELDS = ("code", "kind", "value")

LOCKED_MESSAGE = _(
    "Deze code is inmiddels gebruikt. Code, soort en waarde liggen daarom vast "
    "en zijn niet gewijzigd."
)
DUPLICATE_MESSAGE = _("Deze code bestaat al.")


def discount_code_is_used(code) -> bool:
    """Whether ``code`` was ever used, also by a booking that expired since."""
    if code.pk is None:
        return False
    return code.times_used > 0 or code.redemptions.exists()


def _check_fields(fields):
    unknown = set(fields) - set(EDITABLE_FIELDS)
    if unknown:
        raise ValueError(f"Not editable: {', '.join(sorted(unknown))}")


def _normalized(fields):
    fields = dict(fields)
    if "code" in fields:
        fields["code"] = normalize_discount_code(fields["code"])
    return fields


def _validate(code):
    """Raise ``ValidationError`` (a dict per field) if ``code`` breaks a rule."""
    errors = {}
    try:
        # The field validators: format of the code, digits, required fields.
        code.full_clean(validate_unique=False)
    except ValidationError as error:
        errors = error.update_error_dict(errors)

    def add(field, message, error_code, params=None):
        errors.setdefault(field, []).append(
            ValidationError(message, code=error_code, params=params)
        )

    if code.code and "code" not in errors:
        others = DiscountCode.objects.filter(code=code.code).exclude(pk=code.pk)
        if others.exists():
            add("code", DUPLICATE_MESSAGE, "duplicate")
    if code.value is not None and "value" not in errors:
        if code.value <= 0:
            add("value", _("De waarde moet groter dan 0 zijn."), "not_positive")
        elif code.kind == DiscountKind.PERCENT and code.value > 100:
            add("value", _("Een percentage is hoogstens 100."), "percent_too_high")
    if (
        code.valid_from
        and code.valid_until
        and code.valid_until < code.valid_from
        and "valid_until" not in errors
    ):
        add(
            "valid_until",
            _("De einddatum mag niet voor de begindatum liggen."),
            "until_before_from",
        )
    if code.max_uses is not None and "max_uses" not in errors:
        if code.max_uses < 1:
            add(
                "max_uses",
                _(
                    "Vul minstens 1 in, of laat het veld leeg voor onbeperkt. "
                    "Zet “Actief” uit om de code te stoppen."
                ),
                "min_one",
            )
        elif code.max_uses < code.times_used:
            add(
                "max_uses",
                _(
                    "De code is al %(count)s keer gebruikt; het maximum kan niet "
                    "lager zijn."
                ),
                "below_uses",
                {"count": code.times_used},
            )
    if errors:
        raise ValidationError(errors)


def create_discount_code(fields, *, user) -> DiscountCode:
    """
    A new discount code from ``fields`` (see :data:`EDITABLE_FIELDS`), made by
    ``user`` (a ``User`` or ``None``). Raises ``ValidationError``.
    """
    _check_fields(fields)
    code = DiscountCode(created_by=user, **_normalized(fields))
    _validate(code)
    try:
        with transaction.atomic():
            code.save(force_insert=True)
    except IntegrityError:
        # The same code was added since the check.
        raise ValidationError(
            {"code": [ValidationError(DUPLICATE_MESSAGE, code="duplicate")]}
        )
    logger.info(
        "discount code created user=%s code=%s",
        getattr(user, "pk", None),
        code.pk,
    )
    return code


def update_discount_code(pk, changes, *, user) -> DiscountCode:
    """
    Change the discount code ``pk`` with ``changes`` (field: new value, see
    :data:`EDITABLE_FIELDS`). The row is locked while the rules are checked
    against its current uses, and only the changed fields are written.

    Raises ``DiscountCode.DoesNotExist`` and ``ValidationError``.
    """
    _check_fields(changes)
    changes = _normalized(changes)
    with transaction.atomic():
        code = DiscountCode.objects.select_for_update().get(pk=pk)
        if discount_code_is_used(code) and any(
            name in changes and changes[name] != getattr(code, name)
            for name in LOCKED_FIELDS
        ):
            raise ValidationError(LOCKED_MESSAGE, code="locked")
        for name, value in changes.items():
            setattr(code, name, value)
        _validate(code)
        # Never times_used: a guest may have used the code since it was read.
        code.save(update_fields=list(changes))
    logger.info(
        "discount code changed user=%s code=%s fields=%s",
        getattr(user, "pk", None),
        code.pk,
        ",".join(sorted(changes)) or "-",
    )
    return code
