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
