"""
Production environment settings module.
Tweaks the base settings so that caching mechanisms are used where possible,
and HTTPS is leveraged where possible to further secure things.
"""
import os

from django.core.exceptions import ImproperlyConfigured

os.environ.setdefault("ENVIRONMENT", "production")
# NOTE: watch out for multiple projects using the same cache!
os.environ.setdefault("CACHE_DEFAULT", "127.0.0.1:6379/2")

from .base import *  # noqa isort:skip

# Make use of persistent connections for better database performance
# If not specified database connections are closed at the end of each request
for db_config in DATABASES.values():
    db_config["CONN_MAX_AGE"] = config(
        "DB_CONN_MAX_AGE", default=0
    )  # Lifetime of a database connection in seconds

# Caching sessions.
SESSION_ENGINE = "django.contrib.sessions.backends.cache"
SESSION_CACHE_ALIAS = "default"

# Caching templates.
TEMPLATES[0]["OPTIONS"]["loaders"] = [
    ("django.template.loaders.cached.Loader", TEMPLATE_LOADERS)
]

# The file storage engine to use when collecting static files with the
# collectstatic management command.
STATICFILES_STORAGE = "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"

# Production logging facility.

# Production logging facility.
handlers = ["console"] if LOG_STDOUT else ["django"]

LOGGING["loggers"].update(
    {
        "django": {"handlers": handlers, "level": "INFO", "propagate": True},
        "django.security.DisallowedHost": {
            "handlers": handlers,
            "level": "CRITICAL",
            "propagate": False,
        },
    }
)

# Only set this when we're behind a reverse proxy
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
# HTTPS only: redirect http, and tell browsers to use https for a year (HSTS).
# conf/docker.py turns the redirect off, because it runs locally on http.
SECURE_SSL_REDIRECT = config("SECURE_SSL_REDIRECT", default=True)
SECURE_HSTS_SECONDS = config("SECURE_HSTS_SECONDS", default=31536000)
SECURE_CONTENT_TYPE_NOSNIFF = True  # Sets X-Content-Type-Options: nosniff
SECURE_BROWSER_XSS_FILTER = True  # Sets X-XSS-Protection: 1; mode=block

#
# Custom settings overrides
#
SHOW_ALERT = False

# Required: the links in the booking mails are built with it. No default, so a
# missing SITE_URL stops the start instead of sending links to localhost.
SITE_URL = config("SITE_URL")
# The links contain the token of a booking, so they must be https. Applies to
# everything that builds on these settings (staging, test, docker), unless it is
# turned off explicitly with SITE_URL_REQUIRE_HTTPS=no; conf/docker.py does that
# because it runs locally on http.
SITE_URL_REQUIRE_HTTPS = config("SITE_URL_REQUIRE_HTTPS", default=True)
if SITE_URL_REQUIRE_HTTPS and not SITE_URL.startswith("https://"):
    raise ImproperlyConfigured(
        f"SITE_URL must start with https://, not {SITE_URL!r}. "
        "Set SITE_URL_REQUIRE_HTTPS=no only for a local http setup."
    )

##############################
#                            #
# 3RD PARTY LIBRARY SETTINGS #
#                            #
##############################

# APM
MIDDLEWARE = ["elasticapm.contrib.django.middleware.TracingMiddleware"] + MIDDLEWARE
INSTALLED_APPS = INSTALLED_APPS + [
    "elasticapm.contrib.django",
]

if SUBPATH and SUBPATH != "/":
    STATIC_URL = f"{SUBPATH}{STATIC_URL}"
    MEDIA_URL = f"{SUBPATH}{MEDIA_URL}"
