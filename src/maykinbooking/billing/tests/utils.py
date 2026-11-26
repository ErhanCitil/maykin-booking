"""
Helpers for tests with money: a temporary MEDIA_ROOT for invoice PDFs, Mollie
answers for ``requests_mock``, and booking shortcuts.
"""
import shutil
import tempfile
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from urllib.parse import urlsplit

from django.db import connection
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from maykinbooking.bookings.services.booking import Actor, PaymentMethod, create_booking

from ..models import FakeProviderPayment, PaymentState

MOLLIE_API = "https://api.mollie.com/v2"
# Not a real key: the tests never reach Mollie (requests_mock).
MOLLIE_TEST_KEY = "test_not-a-real-key-for-the-tests"

GUEST = {
    "first_name": "Alex",
    "last_name": "de Landgraaf",
    "email": "alex@example.com",
    "address": "Keizersgracht 117",
    "zipcode": "1015CJ",
    "city": "Amsterdam",
    "country": "NL",
}


class TempMediaMixin:
    """
    ``MEDIA_ROOT`` and ``PRIVATE_MEDIA_ROOT`` are temporary directories for the
    whole test class, so the invoice PDFs and uploads that the tests store are
    removed afterwards. ``private_media_root`` is the one of the invoices.

    For a test that counts files, use :func:`temp_private_media` (a directory
    of its own), so the order of the tests does not matter.
    """

    @classmethod
    def setUpClass(cls):
        cls.media_root = tempfile.mkdtemp(prefix="maykinbooking-test-media-")
        cls.private_media_root = tempfile.mkdtemp(
            prefix="maykinbooking-test-private-media-"
        )
        cls._media_override = override_settings(
            MEDIA_ROOT=cls.media_root, PRIVATE_MEDIA_ROOT=cls.private_media_root
        )
        cls._media_override.enable()
        try:
            super().setUpClass()
        except Exception:
            cls._remove_media()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            super().tearDownClass()
        finally:
            cls._remove_media()

    @classmethod
    def _remove_media(cls):
        cls._media_override.disable()
        shutil.rmtree(cls.media_root, ignore_errors=True)
        shutil.rmtree(cls.private_media_root, ignore_errors=True)


@contextmanager
def temp_private_media():
    """A ``PRIVATE_MEDIA_ROOT`` of its own for the block; yields its path."""
    directory = tempfile.mkdtemp(prefix="maykinbooking-test-private-media-")
    try:
        with override_settings(PRIVATE_MEDIA_ROOT=directory):
            yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@contextmanager
def commit_hooks():
    """
    Run the ``transaction.on_commit`` callbacks of the block afterwards, like a
    commit, inside a ``TestCase``. Unlike ``captureOnCommitCallbacks`` in Django
    3.2, the callbacks that those callbacks register are run as well: after a
    real commit a refund (in a callback) sends its mail (in a callback).
    Nothing runs when the block raises.
    """
    start = len(connection.run_on_commit)
    yield
    while len(connection.run_on_commit) > start:
        pending = connection.run_on_commit[start:]
        del connection.run_on_commit[start:]
        for _savepoints, callback in pending:
            callback()


def in_days(days):
    return timezone.localdate() + timedelta(days=days)


def book(
    room,
    start,
    end,
    *,
    payment_method=PaymentMethod.AT_HOTEL,
    guest=None,
    actor=None,
    **kwargs,
):
    """``create_booking`` with a guest and the defaults of the checkout."""
    return create_booking(
        room_id=room.pk,
        start=start,
        end=end,
        guest=guest or GUEST,
        language="nl",
        actor=actor or Actor.guest(),
        payment_method=payment_method,
        **kwargs,
    )


def path_of(url):
    """The path (and query) of an absolute URL, for the test client."""
    parts = urlsplit(url)
    return parts.path + (f"?{parts.query}" if parts.query else "")


def pay_at_provider(payment, status=PaymentState.PAID):
    """The guest pays (or not) at the fake provider; we have not heard yet."""
    paid = status == PaymentState.PAID
    FakeProviderPayment.objects.filter(pk=payment.provider_id).update(
        status=status,
        method="ideal" if paid else "",
        paid_at=timezone.now() if paid else None,
    )


def fake_checkout_path(payment):
    return reverse("billing:fake_checkout", args=[payment.provider_id])


def mollie_payment(
    payment_id="tr_WDqYK6vllg",
    *,
    status="open",
    amount="340.00",
    currency="EUR",
    paid_at=None,
    metadata=None,
    checkout_url="https://www.mollie.com/checkout/select-method/WDqYK6vllg",
):
    """A payment as the Mollie API returns it."""
    data = {
        "resource": "payment",
        "id": payment_id,
        "mode": "test",
        "status": status,
        "amount": {"value": amount, "currency": currency},
        "description": "Boeking",
        "method": "ideal" if status == "paid" else None,
        "metadata": metadata or {},
        "_links": {},
    }
    if status == "open":
        data["_links"]["checkout"] = {"href": checkout_url, "type": "text/html"}
    if paid_at is not None:
        data["paidAt"] = paid_at
    return data


def mollie_refund(
    refund_id="re_4qqhO89gsT",
    *,
    status="pending",
    amount="340.00",
    currency="EUR",
    metadata=None,
):
    """A refund as the Mollie API returns it."""
    return {
        "resource": "refund",
        "id": refund_id,
        "status": status,
        "amount": {"value": amount, "currency": currency},
        "metadata": metadata or {},
    }


def mollie_refund_list(*refunds):
    return {"count": len(refunds), "_embedded": {"refunds": list(refunds)}}


def money(value):
    return Decimal(value)
