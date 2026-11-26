"""
A small client for the Mollie REST API v2 (payments and refunds), on
``requests``: https://docs.mollie.com/reference/v2/payments-api/create-payment

Only test mode: the API key must start with ``test_``. The key is sent in the
``Authorization`` header and never appears in an error message or log line.
"""
import re
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ImproperlyConfigured
from django.utils.dateparse import parse_datetime

import requests

from ..models import ProviderName
from .base import PaymentProvider, ProviderError, ProviderPayment, ProviderRefund

API_URL = "https://api.mollie.com/v2"
# (connect, read) in seconds.
TIMEOUT = (5, 15)
# Mollie allows 250 refunds per page; a payment never has that many.
REFUND_PAGE_SIZE = 250
PAYMENT_ID = re.compile(r"tr_[A-Za-z0-9]{1,60}")
# Mollie's error detail is quoted in the message, up to this many characters.
MAX_DETAIL_LENGTH = 200


def _money(amount, currency):
    return {"currency": currency, "value": f"{Decimal(amount):.2f}"}


def _metadata(data):
    metadata = data.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _parse_payment(data):
    try:
        amount = data["amount"]
        paid_at = data.get("paidAt")
        return ProviderPayment(
            id=str(data["id"]),
            status=str(data["status"]),
            amount=Decimal(str(amount["value"])),
            currency=str(amount["currency"]),
            checkout_url=str(
                ((data.get("_links") or {}).get("checkout") or {}).get("href") or ""
            ),
            method=str(data.get("method") or ""),
            paid_at=parse_datetime(paid_at) if paid_at else None,
            metadata=_metadata(data),
        )
    except (KeyError, TypeError, ValueError, InvalidOperation, AttributeError):
        raise ProviderError("Mollie sent an unexpected payment.", retryable=True)


def _parse_refund(data):
    try:
        amount = data["amount"]
        return ProviderRefund(
            id=str(data["id"]),
            status=str(data["status"]),
            amount=Decimal(str(amount["value"])),
            currency=str(amount["currency"]),
            metadata=_metadata(data),
        )
    except (KeyError, TypeError, ValueError, InvalidOperation, AttributeError):
        raise ProviderError("Mollie sent an unexpected refund.", retryable=True)


class MollieProvider(PaymentProvider):
    name = ProviderName.MOLLIE

    def __init__(self, api_key, *, session=None):
        if not api_key.startswith("test_"):
            raise ImproperlyConfigured(
                "Only Mollie test API keys (test_...) are supported."
            )
        self._api_key = api_key
        self._session = session or requests.Session()
        # The key lives on the session, not in every call (and never in a
        # message). No proxies, .netrc or CA bundle from the environment.
        self._session.trust_env = False
        self._session.headers.update(
            {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        )

    def __repr__(self):
        return "<MollieProvider>"

    def _check_payment_id(self, payment_id):
        # It goes into the URL path: nothing but a Mollie payment id.
        if not isinstance(payment_id, str) or not PAYMENT_ID.fullmatch(payment_id):
            raise ProviderError("Not a Mollie payment id.", retryable=False)

    def _scrub(self, text):
        return text.replace(self._api_key, "[API key]")

    def _request(self, method, path, *, body=None, params=None, idempotency_key=None):
        headers = {}
        if idempotency_key:
            headers["Idempotency-Key"] = str(idempotency_key)
        what = f"Mollie {method} {path}"
        try:
            response = self._session.request(
                method,
                API_URL + path,
                json=body,
                params=params,
                headers=headers,
                timeout=TIMEOUT,
                # A redirect would take the Authorization header elsewhere.
                allow_redirects=False,
            )
        except (requests.exceptions.InvalidJSONError, TypeError, ValueError) as exc:
            # A body that is not JSON (e.g. a Decimal in the metadata): the
            # same request fails again.
            raise ProviderError(
                f"{what}: invalid request ({type(exc).__name__}).", retryable=False
            ) from None
        except requests.Timeout:
            raise ProviderError(f"{what}: timeout.", retryable=True) from None
        except requests.RequestException as exc:
            raise ProviderError(
                f"{what}: {type(exc).__name__}.", retryable=True
            ) from None

        status = response.status_code
        if 300 <= status < 400:
            raise ProviderError(
                f"{what}: unexpected redirect (HTTP {status}).",
                retryable=False,
                status_code=status,
            )
        if status >= 400:
            try:
                detail = str(response.json().get("detail", ""))
            except (ValueError, AttributeError):
                detail = ""
            message = f"{what}: HTTP {status}"
            if detail:
                message += f" ({detail[:MAX_DETAIL_LENGTH]})"
            # 4xx: our request is wrong, the same request fails again. Except for
            # 429 (too many requests), which is worth another try later.
            raise ProviderError(
                self._scrub(message),
                retryable=status >= 500 or status == 429,
                status_code=status,
            )
        try:
            data = response.json()
        except (TypeError, ValueError):
            raise ProviderError(
                f"{what}: no JSON in the answer.", retryable=True, status_code=status
            ) from None
        if not isinstance(data, dict):
            raise ProviderError(
                f"{what}: unexpected answer.", retryable=True, status_code=status
            )
        return data

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
        body = {
            "amount": _money(amount, currency),
            "description": description[:255],
            "redirectUrl": redirect_url,
            "method": "ideal",
            "metadata": metadata,
        }
        # Mollie must be able to reach the webhook; locally there is none.
        if webhook_url is not None:
            body["webhookUrl"] = webhook_url
        data = self._request(
            "POST", "/payments", body=body, idempotency_key=idempotency_key
        )
        return _parse_payment(data)

    def get_payment(self, payment_id):
        self._check_payment_id(payment_id)
        return _parse_payment(self._request("GET", f"/payments/{payment_id}"))

    def create_refund(
        self, payment_id, *, amount, currency, description, metadata, idempotency_key
    ):
        self._check_payment_id(payment_id)
        body = {
            "amount": _money(amount, currency),
            "description": description[:255],
            "metadata": metadata,
        }
        data = self._request(
            "POST",
            f"/payments/{payment_id}/refunds",
            body=body,
            idempotency_key=idempotency_key,
        )
        return _parse_refund(data)

    def list_refunds(self, payment_id):
        self._check_payment_id(payment_id)
        data = self._request(
            "GET",
            f"/payments/{payment_id}/refunds",
            params={"limit": REFUND_PAGE_SIZE},
        )
        refunds = (data.get("_embedded") or {}).get("refunds") or []
        if not isinstance(refunds, list):
            raise ProviderError(
                "Mollie sent an unexpected refund list.", retryable=True
            )
        return [_parse_refund(refund) for refund in refunds]
