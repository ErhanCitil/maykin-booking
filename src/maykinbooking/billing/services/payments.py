"""
Online payments and refunds.

The flow of an online booking:

1. ``create_booking(payment_method=ONLINE)``: the order waits for payment and
   holds the room for ``PAYMENT_HOLD_MINUTES``.
2. :func:`start_online_payment`: a ``Payment`` at the provider; the guest goes to
   its ``checkout_url``.
3. :func:`sync_payment` asks the provider for the status (from the return URL,
   the webhook and the cron) and applies it to the order once: confirm, issue
   the invoice and send the confirmation; or expire; or refund a payment that
   came too late or twice.

Refunds are made in the transaction that cancels the order and sent to the
provider after the commit (:func:`execute_refund`). A refund that fails is
retried by ``sync_payments`` and from the dashboard (:func:`retry_refund`).

Locks are always taken in this order: Room → Order → Payment/Refund →
DiscountCode → InvoiceSequence. The provider is never called while a lock is
held.
"""
import logging
import re
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.translation import gettext, gettext_lazy as _

from maykinbooking.base.models import EmailKind, Order, OrderStatus, PaymentStatus, Room
from maykinbooking.bookings.services.availability import free_counts
from maykinbooking.bookings.services.booking import (
    Actor,
    BookingError,
    expire_order,
    send_email_on_commit,
    set_payment_status,
    transition,
)
from maykinbooking.utils.transactions import on_commit_safely

from ..models import (
    CLOSED_REFUND_STATES,
    FAILED_PAYMENT_STATES,
    IN_PROGRESS_REFUND_STATES,
    OPEN_PAYMENT_STATES,
    Payment,
    PaymentState,
    ProviderName,
    Refund,
    RefundReason,
    RefundState,
)
from ..providers import ProviderError, get_provider
from .invoicing import issue_invoice

logger = logging.getLogger(__name__)

CURRENCY = "EUR"
# The cron leaves payments alone that were started less than this ago: the guest
# is probably still paying, and the return URL will sync it.
SYNC_MIN_AGE = timedelta(minutes=2)
# A pending order whose payment is still open at the provider is not expired
# until this long after its hold: the guest may be paying at the bank.
PAYMENT_GRACE = timedelta(minutes=15)
# When the provider cannot be reached, a pending order is not expired (it may be
# paid) until this long after its hold.
PROVIDER_DOWN_GRACE = timedelta(hours=24)
# Refunds that failed are retried automatically this many times; staff can
# always try again from the dashboard.
MAX_REFUND_ATTEMPTS = 5
MAX_ERROR_LENGTH = 500
# The guest is only sent to Mollie (https, mollie.com or a subdomain) or to this
# site (the fake provider).
CHECKOUT_HOSTS = ("mollie.com",)
# Never in a URL the guest is sent to: backslashes, whitespace, control chars.
UNSAFE_URL_CHARACTERS = re.compile(r"[\\\s\x00-\x1f\x7f]")

PAYMENT_STATE_REASONS = {
    PaymentState.FAILED: _("Betaling mislukt"),
    PaymentState.CANCELED: _("Betaling geannuleerd"),
    PaymentState.EXPIRED: _("Betaling verlopen"),
}


class PaymentStartFailed(BookingError):
    """The payment could not be started. The order has expired."""


def _error_text(exc):
    return f"{type(exc).__name__}: {exc}"[:MAX_ERROR_LENGTH]


def return_url(payment):
    """Where the provider sends the guest back to (absolute, from ``SITE_URL``)."""
    path = reverse("billing:payment_return", kwargs={"reference": payment.reference})
    return settings.SITE_URL.rstrip("/") + path


def webhook_url():
    """
    The webhook for Mollie, or ``None`` when ``SITE_URL`` is not https: Mollie
    cannot reach a local site, the return URL and the cron do the work then.
    """
    if not settings.SITE_URL.startswith("https://"):
        return None
    return settings.SITE_URL.rstrip("/") + reverse("billing:payment_webhook")


def checkout_url_allowed(url):
    """
    Whether the guest may be redirected to ``url``: https (default port) on
    mollie.com or a subdomain, or this site itself as in ``SITE_URL`` (the fake
    provider).

    Refused as well: backslashes, whitespace and control characters anywhere
    (browsers read those differently than ``urlsplit``: ``evil.com\\@mollie.com``
    goes to evil.com), a user name or password, a host that is not plain ASCII
    and an invalid port.
    """
    if not isinstance(url, str) or not url or UNSAFE_URL_CHARACTERS.search(url):
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if "@" in parts.netloc or parts.username is not None:
        return False
    host = (parts.hostname or "").lower()
    if not host or not host.isascii():
        return False
    site = urlsplit(settings.SITE_URL)
    if parts.scheme in ("http", "https") and (parts.scheme, host, port) == (
        site.scheme,
        (site.hostname or "").lower(),
        site.port,
    ):
        return True
    if parts.scheme != "https" or port not in (None, 443):
        return False
    return any(
        host == allowed or host.endswith(f".{allowed}") for allowed in CHECKOUT_HOSTS
    )


def start_online_payment(order_id) -> Payment:
    """
    Start the payment of an order that waits for payment. Returns the
    ``Payment``; redirect the guest to its ``checkout_url`` (always allowed by
    :func:`checkout_url_allowed`).

    If the provider fails, the payment gets status ``error``, the order expires
    (room and discount code are free again) and ``PaymentStartFailed`` is
    raised.
    """
    failed = None
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order_id)
        if (
            order.status != OrderStatus.PENDING_PAYMENT
            or order.payment_status != PaymentStatus.OPEN
        ):
            raise PaymentStartFailed(_("Deze boeking wacht niet op een betaling."))
        # Started before (a confirmation sent twice, a guest coming back): the
        # same payment page, no second payment.
        existing = (
            order.payments.filter(
                status__in=OPEN_PAYMENT_STATES, processed_at__isnull=True
            )
            .exclude(provider_id="")
            .order_by("-created_at", "-pk")
            .first()
        )
        if existing is not None and checkout_url_allowed(existing.checkout_url):
            return existing
        try:
            provider = get_provider()
            if order.total_price <= 0:
                raise ProviderError("Nothing to pay.", retryable=False)
        except ProviderError as exc:
            # The expiry is committed; the caller hears about it afterwards.
            logger.error("Payment of order %s not started: %s", order.pk, exc)
            expire_order(
                order,
                actor=Actor.system(),
                reason=gettext("Betaling kon niet gestart worden"),
            )
            failed = exc
        else:
            payment = Payment.objects.create(
                order=order,
                provider=provider.name,
                amount=order.total_price,
                currency=CURRENCY,
            )
    if failed is not None:
        raise PaymentStartFailed(
            _("Online betalen is nu niet mogelijk. Probeer het later opnieuw.")
        ) from failed

    try:
        with translation.override(order.language or settings.LANGUAGE_CODE):
            description = gettext("Boeking #%(number)s") % {"number": order.pk}
        remote = provider.create_payment(
            amount=payment.amount,
            currency=payment.currency,
            description=description,
            redirect_url=return_url(payment),
            webhook_url=webhook_url() if provider.name == ProviderName.MOLLIE else None,
            metadata={"order": order.pk, "reference": str(payment.reference)},
            idempotency_key=str(payment.reference),
        )
        if remote.amount != payment.amount or remote.currency != payment.currency:
            raise ProviderError("The provider changed the amount.", retryable=False)
        if not checkout_url_allowed(remote.checkout_url):
            raise ProviderError("The checkout URL is not on an allowed host.")
    except ProviderError as exc:
        _payment_start_failed(order.pk, payment.pk, exc)
        raise PaymentStartFailed(
            _("Online betalen is nu niet mogelijk. Probeer het later opnieuw.")
        ) from exc

    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment.pk)
        payment.provider_id = remote.id
        payment.checkout_url = remote.checkout_url
        if remote.status in OPEN_PAYMENT_STATES:
            payment.status = remote.status
        else:
            payment.status = PaymentState.OPEN
        payment.save(
            update_fields=["provider_id", "checkout_url", "status", "updated_at"]
        )
    return payment


def _payment_start_failed(order_id, payment_id, exc):
    logger.error(
        "Starting payment %s of order %s failed: %s", payment_id, order_id, exc
    )
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order_id)
        payment = Payment.objects.select_for_update().get(pk=payment_id)
        payment.status = PaymentState.ERROR
        payment.last_error = _error_text(exc)
        payment.processed_at = timezone.now()
        payment.save(
            update_fields=["status", "last_error", "processed_at", "updated_at"]
        )
        if order.status == OrderStatus.PENDING_PAYMENT:
            expire_order(
                order,
                actor=Actor.system(),
                reason=gettext("Betaling kon niet gestart worden"),
            )


def sync_payment(payment_id) -> Payment:
    """
    Ask the provider for the status of a payment and apply it to the order.

    Idempotent: a payment is processed once (``processed_at``); after that this
    returns it without calling the provider. Raises ``ProviderError`` when the
    provider cannot be reached (nothing changes then).

    * The amount or currency differs from the payment or the order: logged as an
      error, never confirmed.
    * Paid: a pending order whose hold still runs, or whose room is still free,
      is confirmed and paid, gets its invoice and the confirmation mail. A
      pending order whose room was taken meanwhile expires and the payment is
      refunded (late payment), and so is a payment for an order that expired,
      or that was already paid by another payment (duplicate).
    * Failed, canceled or expired: a pending order without other open payments
      expires; its room and discount code are free again.
    """
    payment = Payment.objects.select_related("order").get(pk=payment_id)
    if payment.processed_at is not None or not payment.provider_id:
        return payment
    remote = get_provider(payment.provider).get_payment(payment.provider_id)

    with transaction.atomic():
        room = Room.objects.select_for_update().get(pk=payment.order.room_id)
        order = Order.objects.select_for_update().get(pk=payment.order_id)
        payment = Payment.objects.select_for_update().get(pk=payment_id)
        if payment.processed_at is not None:
            return payment
        refund = _apply_remote_payment(room, order, payment, remote)
        if refund is not None:
            on_commit_safely(execute_refund, refund.pk)
    return payment


def _apply_remote_payment(room, order, payment, remote):
    """Apply the provider's state of ``payment``. Returns a new refund, or None."""
    now = timezone.now()
    if (
        remote.id != payment.provider_id
        or remote.amount != payment.amount
        or remote.currency != payment.currency
        or payment.amount != order.total_price
    ):
        logger.error(
            "Payment %s of order %s: %s %s at the provider, %s %s expected "
            "(order total %s); not processed",
            payment.pk,
            order.pk,
            remote.amount,
            remote.currency,
            payment.amount,
            payment.currency,
            order.total_price,
        )
        payment.status = PaymentState.ERROR
        payment.last_error = (
            f"Amount or currency differs: {remote.amount} {remote.currency}"
        )
        payment.processed_at = now
        payment.save()
        _expire_if_unpaid(order, payment, reason=gettext("Betaling klopt niet"))
        if remote.status == PaymentState.PAID:
            # The money is there: it must not stay with us unnoticed.
            return _mismatch_refund(order, payment, remote)
        return None

    status = remote.status
    if status not in PaymentState.values or status in (
        PaymentState.CREATED,
        PaymentState.ERROR,
    ):
        logger.warning(
            "Payment %s: unknown status %r at the provider", payment.pk, status
        )
        status = payment.status
    payment.status = status
    payment.method = remote.method or payment.method
    if remote.paid_at is not None:
        payment.paid_at = remote.paid_at

    if status == PaymentState.PAID:
        payment.paid_at = payment.paid_at or now
        payment.processed_at = now
        payment.save()
        return _payment_paid(room, order, payment, now)
    if status in FAILED_PAYMENT_STATES:
        payment.processed_at = now
        payment.save()
        _expire_if_unpaid(order, payment, reason=str(PAYMENT_STATE_REASONS[status]))
        return None
    payment.save()
    return None


def _payment_paid(room, order, payment, now):
    system = Actor.system()
    if order.status == OrderStatus.PENDING_PAYMENT:
        hold_runs = order.hold_expires_at is not None and order.hold_expires_at > now
        # Once the hold has passed the order no longer occupies its room, so a
        # free room means it is still ours to take.
        if (
            hold_runs
            or free_counts([room], order.start_date, order.end_date)[room.pk] >= 1
        ):
            transition(
                order, OrderStatus.CONFIRMED, actor=system, reason=gettext("Betaald")
            )
            set_payment_status(
                order, PaymentStatus.PAID, actor=system, reason=gettext("Betaald")
            )
            order.hold_expires_at = None
            order.save(update_fields=["hold_expires_at"])
            issue_invoice(order)
            send_email_on_commit(order, EmailKind.BOOKING_CONFIRMATION, actor=system)
            return None
        reason = gettext("Betaald na het verlopen van de reservering; kamer bezet")
        set_payment_status(order, PaymentStatus.PAID, actor=system, reason=reason)
        expire_order(order, actor=system, reason=reason)
        return _new_refund(order, payment, RefundReason.LATE_PAYMENT)

    if order.status == OrderStatus.EXPIRED:
        if order.payment_status in (PaymentStatus.OPEN, PaymentStatus.UNPAID):
            set_payment_status(
                order,
                PaymentStatus.PAID,
                actor=system,
                reason=gettext("Betaald na het verlopen van de reservering"),
            )
        return _new_refund(order, payment, RefundReason.LATE_PAYMENT)
    # Confirmed or cancelled: already paid by another payment.
    return _new_refund(order, payment, RefundReason.DUPLICATE)


def _mismatch_refund(order, payment, remote):
    """A refund of what was paid of a payment whose amount did not match."""
    if remote.amount <= 0:
        logger.error(
            "Payment %s of order %s: paid %s %s at the provider; nothing to refund",
            payment.pk,
            order.pk,
            remote.amount,
            remote.currency,
        )
        return None
    logger.error(
        "Payment %s of order %s: %s %s paid at the provider instead of %s %s; "
        "it is refunded",
        payment.pk,
        order.pk,
        remote.amount,
        remote.currency,
        payment.amount,
        payment.currency,
    )
    return _new_refund(
        order,
        payment,
        RefundReason.MISMATCH,
        amount=remote.amount,
        currency=remote.currency,
    )


def _expire_if_unpaid(order, payment, *, reason):
    """Expire a pending order, unless another payment of it is still open."""
    if order.status != OrderStatus.PENDING_PAYMENT:
        return
    others = order.payments.exclude(pk=payment.pk).filter(
        status__in=OPEN_PAYMENT_STATES, processed_at__isnull=True
    )
    if not others.exists():
        expire_order(order, actor=Actor.system(), reason=reason)


def _new_refund(order, payment, reason, *, actor=None, amount=None, currency=None):
    """
    A refund of all of ``payment`` (or of ``amount`` in ``currency``), unless
    it already has an open one.
    """
    existing = payment.refunds.exclude(status__in=CLOSED_REFUND_STATES).first()
    if existing is not None:
        return existing
    logger.warning(
        "Payment %s of order %s is refunded (%s)", payment.pk, order.pk, reason
    )
    return Refund.objects.create(
        payment=payment,
        amount=payment.amount if amount is None else amount,
        currency=currency or payment.currency,
        reason=reason,
        created_by=actor.user if actor is not None else None,
    )


# Refunds of payments that never paid for the order: made when the payment
# was processed (see _payment_paid and _mismatch_refund).
NOT_CONFIRMING_REASONS = (
    RefundReason.LATE_PAYMENT,
    RefundReason.DUPLICATE,
    RefundReason.MISMATCH,
)


def confirming_payment(order):
    """
    The payment that paid for ``order`` (that confirmed it), or ``None``: the
    paid one that was not refunded as a late, duplicate or other-amount
    payment when it came in.
    """
    return (
        order.payments.filter(status=PaymentState.PAID)
        .exclude(refunds__reason__in=NOT_CONFIRMING_REASONS)
        .order_by("processed_at", "pk")
        .first()
    )


def request_refund(order, *, reason, actor) -> Refund:
    """
    A refund of the payment that paid for ``order`` (see
    :func:`confirming_payment`), unless it is refunded already (or on its
    way); ``None`` then, or when the order was not paid online. Call it in the
    transaction that locked the order (e.g. the cancellation); send it after
    the commit with :func:`execute_refund`.
    """
    payment = confirming_payment(order)
    if payment is None:
        return None
    payment = Payment.objects.select_for_update().get(pk=payment.pk)
    if payment.refunds.exclude(status__in=CLOSED_REFUND_STATES).exists():
        return None
    return _new_refund(order, payment, reason, actor=actor)


def _refund_refused_refunds(order, *, actor):
    """
    New refunds for the payments of ``order`` whose refunds were all refused
    by the provider (failed, canceled): the same amount and reason. Returns the
    last one made, or ``None``.
    """
    made = None
    for payment in order.payments.select_for_update().order_by("pk"):
        refunds = list(payment.refunds.order_by("created_at", "pk"))
        if refunds and all(r.status in CLOSED_REFUND_STATES for r in refunds):
            last = refunds[-1]
            made = _new_refund(
                order,
                payment,
                last.reason,
                actor=actor,
                amount=last.amount,
                currency=last.currency,
            )
    return made


def execute_refund(refund_id) -> Refund:
    """
    Send a requested (or failed) refund to the provider. Never raises for a
    provider error: the refund gets status ``error`` with the error, one more
    attempt, and an error in the log; the order stays as it is.

    A refund that the provider already has (an earlier attempt timed out after
    it was made) is taken over instead of made again: it is found by the
    reference in its metadata, and the idempotency key is the same.
    """
    refund = Refund.objects.select_related("payment__order").get(pk=refund_id)
    if refund.status not in (RefundState.REQUESTED, RefundState.ERROR):
        return refund
    payment = refund.payment
    order = payment.order
    reference = str(refund.reference)
    try:
        provider = get_provider(payment.provider)
        remote = next(
            (
                existing
                for existing in provider.list_refunds(payment.provider_id)
                if existing.metadata.get("reference") == reference
            ),
            None,
        )
        if remote is None:
            with translation.override(order.language or settings.LANGUAGE_CODE):
                description = gettext("Terugbetaling boeking #%(number)s") % {
                    "number": order.pk
                }
            remote = provider.create_refund(
                payment.provider_id,
                amount=refund.amount,
                currency=refund.currency,
                description=description,
                metadata={"order": order.pk, "reference": reference},
                idempotency_key=reference,
            )
    except ProviderError as exc:
        with transaction.atomic():
            Order.objects.select_for_update().get(pk=order.pk)
            refund = Refund.objects.select_for_update().get(pk=refund_id)
            if refund.status in (RefundState.REQUESTED, RefundState.ERROR):
                refund.status = RefundState.ERROR
                refund.attempts += 1
                refund.error = _error_text(exc)
                refund.save(update_fields=["status", "attempts", "error", "updated_at"])
        logger.error(
            "Refund %s of order %s failed (attempt %s): %s",
            refund.pk,
            order.pk,
            refund.attempts,
            exc,
        )
        return refund

    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order.pk)
        refund = Refund.objects.select_for_update().get(pk=refund_id)
        if refund.status not in (RefundState.REQUESTED, RefundState.ERROR):
            return refund
        refund.attempts += 1
        _apply_remote_refund(order, refund, remote)
    return refund


def _apply_remote_refund(order, refund, remote):
    refund.provider_id = remote.id
    status = remote.status
    if status == RefundState.REFUNDED:
        refund.status = RefundState.REFUNDED
        refund.refunded_at = timezone.now()
        refund.error = ""
        refund.save()
        _refund_completed(order, refund)
    elif status in CLOSED_REFUND_STATES:
        refund.status = status
        refund.error = f"The provider says: {status}"
        refund.save()
        logger.error(
            "Refund %s of order %s is %s at the provider", refund.pk, order.pk, status
        )
    else:
        if status not in IN_PROGRESS_REFUND_STATES:
            logger.warning(
                "Refund %s: unknown status %r at the provider", refund.pk, status
            )
            status = RefundState.PENDING
        refund.status = status
        refund.error = ""
        refund.save()


def _refund_completed(order, refund):
    system = Actor.system()
    # A duplicate payment, or one of another amount, going back does not change
    # what the order cost or whether it was paid.
    if refund.reason not in (RefundReason.DUPLICATE, RefundReason.MISMATCH):
        set_payment_status(
            order, PaymentStatus.REFUNDED, actor=system, reason=gettext("Terugbetaald")
        )
    send_email_on_commit(
        order, EmailKind.PAYMENT_REFUND, actor=system, refund_id=refund.pk
    )


def sync_refund(refund_id) -> Refund:
    """
    Ask the provider about a refund that is on its way (queued, pending,
    processing) and apply it. Raises ``ProviderError``.
    """
    refund = Refund.objects.select_related("payment__order").get(pk=refund_id)
    if refund.status not in IN_PROGRESS_REFUND_STATES:
        return refund
    payment = refund.payment
    remote = next(
        (
            existing
            for existing in get_provider(payment.provider).list_refunds(
                payment.provider_id
            )
            if existing.id == refund.provider_id
        ),
        None,
    )
    if remote is None:
        logger.warning("Refund %s is unknown at the provider", refund.pk)
        return refund
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=payment.order_id)
        refund = Refund.objects.select_for_update().get(pk=refund_id)
        if refund.status in IN_PROGRESS_REFUND_STATES:
            _apply_remote_refund(order, refund, remote)
    return refund


def retry_refund(order_id, *, actor) -> Refund:
    """
    Staff tries a refund of an order again (dashboard): the refund in
    ``error``; else a new one for a payment whose refund the provider refused
    (failed, canceled); else a refund of the payment that paid for a cancelled
    or expired order. Returns the refund, or ``None`` if there is nothing to
    refund. The refund is sent after the commit.
    """
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order_id)
        refund = (
            Refund.objects.select_for_update()
            .filter(
                payment__order=order,
                status__in=[RefundState.ERROR, RefundState.REQUESTED],
            )
            .order_by("-created_at", "-pk")
            .first()
        )
        if refund is None:
            refund = _refund_refused_refunds(order, actor=actor)
        if refund is None and order.payment_status == PaymentStatus.PAID:
            if order.status == OrderStatus.CANCELLED:
                refund = request_refund(
                    order, reason=RefundReason.CANCELLATION, actor=actor
                )
            elif order.status == OrderStatus.EXPIRED:
                refund = request_refund(
                    order, reason=RefundReason.LATE_PAYMENT, actor=actor
                )
        if refund is None:
            return None
        on_commit_safely(execute_refund, refund.pk)
    refund.refresh_from_db()
    return refund


def sync_open_payments(*, now=None) -> int:
    """
    Sync the payments that are still open and were started more than
    ``SYNC_MIN_AGE`` ago (cron). Returns the number synced; provider errors are
    logged and skipped.
    """
    now = now or timezone.now()
    payment_ids = list(
        Payment.objects.filter(
            status__in=OPEN_PAYMENT_STATES,
            processed_at__isnull=True,
            created_at__lte=now - SYNC_MIN_AGE,
        )
        .exclude(provider_id="")
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    synced = 0
    for payment_id in payment_ids:
        try:
            sync_payment(payment_id)
        except ProviderError as exc:
            logger.warning("Syncing payment %s failed: %s", payment_id, exc)
        else:
            synced += 1
    return synced


def sync_pending_order_payments(order_ids, *, now=None) -> set:
    """
    Sync the open payments of the orders ``order_ids`` before they are expired
    (``expire_stale_pending``). Returns the ids of the orders that must not be
    expired this round, because the guest may still pay or have paid:

    * the payment is still open at the provider and the hold passed less than
      ``PAYMENT_GRACE`` ago (the guest is at the bank; a payment that comes in
      then confirms the booking if the room is still free);
    * the provider could not be reached and the hold passed less than
      ``PROVIDER_DOWN_GRACE`` ago.
    """
    now = now or timezone.now()
    skip = set()
    payments = (
        Payment.objects.filter(
            order_id__in=list(order_ids),
            status__in=OPEN_PAYMENT_STATES,
            processed_at__isnull=True,
        )
        .exclude(provider_id="")
        .select_related("order")
        .order_by("pk")
    )
    for payment in payments:
        order = payment.order
        try:
            payment = sync_payment(payment.pk)
        except ProviderError as exc:
            since = order.hold_expires_at or order.created_at or now
            if now - since < PROVIDER_DOWN_GRACE:
                skip.add(order.pk)
                logger.warning(
                    "Payment %s of order %s could not be synced, the order is "
                    "not expired yet: %s",
                    payment.pk,
                    order.pk,
                    exc,
                )
            else:
                logger.error(
                    "Payment %s of order %s could not be synced for %s; the "
                    "order expires anyway: %s",
                    payment.pk,
                    order.pk,
                    PROVIDER_DOWN_GRACE,
                    exc,
                )
            continue
        if (
            payment.status in OPEN_PAYMENT_STATES
            and payment.processed_at is None
            and order.hold_expires_at is not None
            and now < order.hold_expires_at + PAYMENT_GRACE
        ):
            skip.add(order.pk)
    return skip


def sync_refunds(*, now=None) -> int:
    """
    Send the refunds that are requested (more than ``SYNC_MIN_AGE`` ago) or
    failed fewer than ``MAX_REFUND_ATTEMPTS`` times, and ask about the ones on
    their way (cron). Returns the number of refunds handled.
    """
    now = now or timezone.now()
    to_send = list(
        Refund.objects.filter(
            Q(status=RefundState.REQUESTED, created_at__lte=now - SYNC_MIN_AGE)
            | Q(status=RefundState.ERROR, attempts__lt=MAX_REFUND_ATTEMPTS)
        )
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    for refund_id in to_send:
        execute_refund(refund_id)
    on_their_way = list(
        Refund.objects.filter(status__in=IN_PROGRESS_REFUND_STATES)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    for refund_id in on_their_way:
        try:
            sync_refund(refund_id)
        except ProviderError as exc:
            logger.warning("Syncing refund %s failed: %s", refund_id, exc)
    return len(to_send) + len(on_their_way)
