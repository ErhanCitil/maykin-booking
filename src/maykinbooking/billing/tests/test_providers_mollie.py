"""The Mollie client against ``requests_mock``: requests, answers, errors."""
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

import requests
import requests_mock

from ..providers import ProviderError, ProviderNotAvailable, get_provider
from ..providers.fake import FakeProvider
from ..providers.mollie import TIMEOUT, MollieProvider
from .utils import (
    MOLLIE_API,
    MOLLIE_TEST_KEY,
    mollie_payment,
    mollie_refund,
    mollie_refund_list,
)


class MollieRequestTests(SimpleTestCase):
    def setUp(self):
        self.provider = MollieProvider(MOLLIE_TEST_KEY)

    def create(self, **kwargs):
        arguments = {
            "amount": Decimal("340"),
            "currency": "EUR",
            "description": "Boeking #12",
            "redirect_url": "https://booking.example.com/betalen/x/terug/",
            "webhook_url": "https://booking.example.com/betalen/webhook/",
            "metadata": {"order": 12, "reference": "ref"},
            "idempotency_key": "ref-123",
            **kwargs,
        }
        return self.provider.create_payment(**arguments)

    def test_create_payment(self):
        with requests_mock.Mocker() as mocker:
            mocker.post(
                f"{MOLLIE_API}/payments", json=mollie_payment(), status_code=201
            )
            payment = self.create()

        request = mocker.last_request
        self.assertEqual(request.headers["Authorization"], f"Bearer {MOLLIE_TEST_KEY}")
        self.assertEqual(request.headers["Idempotency-Key"], "ref-123")
        self.assertEqual(
            request.json(),
            {
                "amount": {"currency": "EUR", "value": "340.00"},
                "description": "Boeking #12",
                "redirectUrl": "https://booking.example.com/betalen/x/terug/",
                "webhookUrl": "https://booking.example.com/betalen/webhook/",
                "method": "ideal",
                "metadata": {"order": 12, "reference": "ref"},
            },
        )
        self.assertEqual(request.timeout, TIMEOUT)
        self.assertEqual(payment.id, "tr_WDqYK6vllg")
        self.assertEqual(payment.status, "open")
        self.assertEqual(payment.amount, Decimal("340.00"))
        self.assertEqual(
            payment.checkout_url,
            "https://www.mollie.com/checkout/select-method/WDqYK6vllg",
        )

    def test_without_webhook_url_it_is_left_out(self):
        with requests_mock.Mocker() as mocker:
            mocker.post(
                f"{MOLLIE_API}/payments", json=mollie_payment(), status_code=201
            )
            self.create(webhook_url=None)
        self.assertNotIn("webhookUrl", mocker.last_request.json())

    def test_get_paid_payment(self):
        with requests_mock.Mocker() as mocker:
            mocker.get(
                f"{MOLLIE_API}/payments/tr_WDqYK6vllg",
                json=mollie_payment(status="paid", paid_at="2027-03-01T09:05:00+00:00"),
            )
            payment = self.provider.get_payment("tr_WDqYK6vllg")
        self.assertEqual(payment.status, "paid")
        self.assertEqual(payment.method, "ideal")
        self.assertEqual(payment.checkout_url, "")
        self.assertEqual(
            payment.paid_at, datetime(2027, 3, 1, 9, 5, tzinfo=dt_timezone.utc)
        )

    def test_refunds(self):
        with requests_mock.Mocker() as mocker:
            mocker.post(
                f"{MOLLIE_API}/payments/tr_WDqYK6vllg/refunds",
                json=mollie_refund(metadata={"reference": "r-1"}),
                status_code=201,
            )
            mocker.get(
                f"{MOLLIE_API}/payments/tr_WDqYK6vllg/refunds",
                json=mollie_refund_list(
                    mollie_refund(status="refunded", metadata={"reference": "r-1"})
                ),
            )
            refund = self.provider.create_refund(
                "tr_WDqYK6vllg",
                amount=Decimal("340.00"),
                currency="EUR",
                description="Terugbetaling boeking #12",
                metadata={"reference": "r-1"},
                idempotency_key="r-1",
            )
            create_request = mocker.request_history[0]
            refunds = self.provider.list_refunds("tr_WDqYK6vllg")

        self.assertEqual(create_request.headers["Idempotency-Key"], "r-1")
        self.assertEqual(
            create_request.json()["amount"], {"currency": "EUR", "value": "340.00"}
        )
        self.assertEqual((refund.id, refund.status), ("re_4qqhO89gsT", "pending"))
        self.assertEqual(mocker.last_request.qs, {"limit": ["250"]})
        self.assertEqual([r.status for r in refunds], ["refunded"])
        self.assertEqual(refunds[0].metadata, {"reference": "r-1"})


class MollieErrorTests(SimpleTestCase):
    def setUp(self):
        self.provider = MollieProvider(MOLLIE_TEST_KEY)
        self.url = f"{MOLLIE_API}/payments/tr_WDqYK6vllg"

    def get_error(self, **mock_kwargs):
        with requests_mock.Mocker() as mocker:
            mocker.get(self.url, **mock_kwargs)
            with self.assertRaises(ProviderError) as error:
                self.provider.get_payment("tr_WDqYK6vllg")
        return error.exception

    def test_4xx_is_not_retryable(self):
        error = self.get_error(
            status_code=422, json={"status": 422, "detail": "The amount is too low"}
        )
        self.assertFalse(error.retryable)
        self.assertEqual(error.status_code, 422)
        self.assertIn("The amount is too low", str(error))

    def test_5xx_timeouts_and_connection_errors_are_retryable(self):
        for kwargs in [
            {"status_code": 503, "text": "unavailable"},
            {"exc": requests.exceptions.ConnectTimeout},
            {"exc": requests.exceptions.ReadTimeout},
            {"exc": requests.exceptions.ConnectionError},
            {"status_code": 200, "text": "<html>not json</html>"},
            {"status_code": 429, "json": {"detail": "slow down"}},
        ]:
            with self.subTest(kwargs=kwargs):
                self.assertTrue(self.get_error(**kwargs).retryable)

    def test_the_key_is_never_in_an_error(self):
        error = self.get_error(
            status_code=401,
            json={"detail": f"Invalid API key: '{MOLLIE_TEST_KEY}'"},
        )
        self.assertNotIn(MOLLIE_TEST_KEY, str(error))
        self.assertNotIn(MOLLIE_TEST_KEY, repr(self.provider))
        self.assertIsNone(error.__cause__)

    def test_unexpected_answer(self):
        error = self.get_error(json={"id": "tr_WDqYK6vllg"})
        self.assertTrue(error.retryable)

    def test_only_mollie_payment_ids_go_into_the_url(self):
        with requests_mock.Mocker() as mocker:
            for payment_id in ["../customers", "tr_1/refunds", "", "re_123", None]:
                with self.subTest(payment_id=payment_id):
                    with self.assertRaises(ProviderError):
                        self.provider.get_payment(payment_id)
        self.assertEqual(mocker.call_count, 0)

    def test_the_session_has_the_key_and_ignores_the_environment(self):
        session = self.provider._session
        self.assertEqual(session.headers["Authorization"], f"Bearer {MOLLIE_TEST_KEY}")
        self.assertIs(session.trust_env, False)
        with requests_mock.Mocker() as mocker:
            mocker.get(self.url, json=mollie_payment())
            self.provider.get_payment("tr_WDqYK6vllg")
        self.assertEqual(
            mocker.last_request.headers["Authorization"], f"Bearer {MOLLIE_TEST_KEY}"
        )

    def test_redirects_are_not_followed(self):
        with requests_mock.Mocker() as mocker:
            mocker.get(
                self.url,
                status_code=302,
                headers={"Location": "https://evil.example.com/steal"},
            )
            mocker.get("https://evil.example.com/steal", json=mollie_payment())
            with self.assertRaises(ProviderError) as error:
                self.provider.get_payment("tr_WDqYK6vllg")
        self.assertEqual(mocker.call_count, 1)
        self.assertFalse(error.exception.retryable)
        self.assertEqual(error.exception.status_code, 302)

    def test_a_body_that_is_not_json_is_a_provider_error(self):
        with requests_mock.Mocker() as mocker:
            mocker.post(f"{MOLLIE_API}/payments", json=mollie_payment())
            with self.assertRaises(ProviderError) as error:
                self.provider.create_payment(
                    amount=Decimal("10.00"),
                    currency="EUR",
                    description="x",
                    redirect_url="https://booking.example.com/",
                    webhook_url=None,
                    metadata={"total": Decimal("10.00")},
                    idempotency_key="k",
                )
        self.assertFalse(error.exception.retryable)
        self.assertEqual(mocker.call_count, 0)

    def test_only_test_keys(self):
        for key in ["live_abc", "abc", ""]:
            with self.subTest(key=key):
                with self.assertRaises(ImproperlyConfigured) as error:
                    MollieProvider(key)
                if key:
                    self.assertNotIn(key, str(error.exception))


class GetProviderTests(SimpleTestCase):
    @override_settings(MOLLIE_API_KEY=MOLLIE_TEST_KEY, PAYMENT_FAKE_PROVIDER=True)
    def test_mollie_when_there_is_a_key(self):
        self.assertIsInstance(get_provider(), MollieProvider)
        self.assertIsInstance(get_provider("fake"), FakeProvider)

    @override_settings(MOLLIE_API_KEY="", PAYMENT_FAKE_PROVIDER=True)
    def test_fake_without_a_key(self):
        self.assertIsInstance(get_provider(), FakeProvider)
        with self.assertRaises(ProviderNotAvailable):
            get_provider("mollie")

    @override_settings(MOLLIE_API_KEY="", PAYMENT_FAKE_PROVIDER=False)
    def test_none(self):
        with self.assertRaises(ProviderNotAvailable):
            get_provider()
        with self.assertRaises(ProviderNotAvailable):
            get_provider("fake")
        with self.assertRaises(ProviderNotAvailable):
            get_provider("paypal")
