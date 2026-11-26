"""
The production settings (and those that build on them) refuse a SITE_URL without
https, unless that is turned off explicitly (security N5).

Settings are only read when Django starts, so every case loads them in a new
Python process with a minimal environment.
"""
import os
import subprocess
import sys
from pathlib import Path

from django.test import SimpleTestCase

SRC_DIR = Path(__file__).resolve().parents[3]

LOAD_SETTINGS = (
    "import django; from django.conf import settings; django.setup(); "
    "print(settings.SITE_URL)"
)


class SiteUrlHttpsCheckTests(SimpleTestCase):
    def load(self, settings_module, **env):
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "DJANGO_SETTINGS_MODULE": settings_module,
            "SECRET_KEY": "dummy",
            **env,
        }
        return subprocess.run(
            [sys.executable, "-c", LOAD_SETTINGS],
            cwd=SRC_DIR,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def assertLoads(self, result, site_url):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), site_url)

    def assertRefused(self, result):
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "ImproperlyConfigured: SITE_URL must start with https://", result.stderr
        )

    def test_production_needs_https(self):
        self.assertRefused(
            self.load(
                "maykinbooking.conf.production", SITE_URL="http://booking.example.com"
            )
        )
        self.assertLoads(
            self.load(
                "maykinbooking.conf.production", SITE_URL="https://booking.example.com"
            ),
            "https://booking.example.com",
        )

    def test_does_not_depend_on_the_name_of_the_environment(self):
        for settings_module, environment in [
            ("maykinbooking.conf.production", "prod"),
            ("maykinbooking.conf.staging", "staging"),
        ]:
            with self.subTest(settings_module):
                self.assertRefused(
                    self.load(
                        settings_module,
                        ENVIRONMENT=environment,
                        SITE_URL="http://booking.example.com",
                    )
                )

    def test_can_be_turned_off_explicitly(self):
        self.assertLoads(
            self.load(
                "maykinbooking.conf.staging",
                SITE_URL="http://staging.example.com",
                SITE_URL_REQUIRE_HTTPS="no",
            ),
            "http://staging.example.com",
        )

    def test_docker_runs_locally_on_http(self):
        self.assertLoads(
            self.load("maykinbooking.conf.docker", SITE_URL="http://localhost:8000"),
            "http://localhost:8000",
        )


class PaymentSettingsTests(SimpleTestCase):
    """
    Online payment settings: the fake provider never in production, and only
    Mollie test keys anywhere (live payments are out of scope).
    """

    PRODUCTION = {"SITE_URL": "https://booking.example.com"}

    load = SiteUrlHttpsCheckTests.load
    assertLoads = SiteUrlHttpsCheckTests.assertLoads

    def test_fake_provider_is_refused_in_production(self):
        for settings_module in (
            "maykinbooking.conf.production",
            "maykinbooking.conf.staging",
        ):
            with self.subTest(settings_module):
                result = self.load(
                    settings_module, PAYMENT_FAKE_PROVIDER="yes", **self.PRODUCTION
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("PAYMENT_FAKE_PROVIDER is not allowed", result.stderr)

    def test_only_mollie_test_keys(self):
        key = "live_not-a-real-key-0123456789"
        for settings_module in (
            "maykinbooking.conf.production",
            "maykinbooking.conf.ci",
        ):
            with self.subTest(settings_module):
                result = self.load(
                    settings_module, MOLLIE_API_KEY=key, **self.PRODUCTION
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("MOLLIE_API_KEY must be a Mollie test key", result.stderr)
                self.assertNotIn(key, result.stderr)

    def test_debug_is_refused_in_production(self):
        for settings_module in (
            "maykinbooking.conf.production",
            "maykinbooking.conf.docker",
        ):
            with self.subTest(settings_module):
                result = self.load(settings_module, DEBUG="yes", **self.PRODUCTION)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("DEBUG is not allowed in production", result.stderr)

    def test_private_media_root(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import django; from django.conf import settings; django.setup(); "
                "print(settings.PRIVATE_MEDIA_ROOT); print(settings.MEDIA_ROOT)",
            ],
            cwd=SRC_DIR,
            env={
                "PATH": os.environ.get("PATH", ""),
                "DJANGO_SETTINGS_MODULE": "maykinbooking.conf.production",
                "SECRET_KEY": "dummy",
                "PRIVATE_MEDIA_ROOT": "/srv/invoices",
                **self.PRODUCTION,
            },
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        private, media = result.stdout.splitlines()
        self.assertEqual(private, "/srv/invoices")
        self.assertNotEqual(private, media)

    def test_production_with_a_test_key(self):
        self.assertLoads(
            self.load(
                "maykinbooking.conf.production",
                MOLLIE_API_KEY="test_not-a-real-key",
                **self.PRODUCTION,
            ),
            "https://booking.example.com",
        )
