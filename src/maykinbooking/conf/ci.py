"""
Continuous integration (GitHub Actions) settings module.

Builds on ``base`` instead of ``test``/``production``, because those use Redis for
the cache and sessions and ``ManifestStaticFilesStorage`` (which needs
``collectstatic`` and the frontend bundles). The test suite needs none of that.

The database is configured with the regular ``DB_*`` environment variables, which
the CI workflow points at its PostgreSQL service. ``SECRET_KEY`` must be set in the
environment as well.
"""
import os
import warnings

os.environ.setdefault("DEBUG", "no")
os.environ.setdefault("IS_HTTPS", "no")
os.environ.setdefault("ENVIRONMENT", "ci")
# disable 2FA in tests
os.environ.setdefault("TWO_FACTOR_FORCE_OTP_ADMIN", "no")
os.environ.setdefault("TWO_FACTOR_PATCH_ADMIN", "no")

from .base import *  # noqa isort:skip

# No Redis: in-memory cache and sessions in the database.
# In-memory cache and django-axes don't get along, hence the dummy cache for axes.
# https://django-axes.readthedocs.io/en/latest/configuration.html#known-configuration-problems
CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
    "axes": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"},
}
SESSION_ENGINE = "django.contrib.sessions.backends.db"

# Mails stay in memory (django.core.mail.outbox), nothing is sent.
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# django-extensions is a development tool (requirements/dev.txt), it is not part of
# requirements/ci.txt and the tests don't use it.
INSTALLED_APPS = [app for app in INSTALLED_APPS if app != "django_extensions"]

# THOU SHALT NOT USE NAIVE DATETIMES
warnings.filterwarnings(
    "error",
    r"DateTimeField .* received a naive datetime",
    RuntimeWarning,
    r"django\.db\.models\.fields",
)
