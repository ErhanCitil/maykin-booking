import os

os.environ.setdefault("DB_USER", os.getenv("DATABASE_USER", "postgres"))
os.environ.setdefault("DB_NAME", os.getenv("DATABASE_NAME", "postgres"))
os.environ.setdefault("DB_PASSWORD", os.getenv("DATABASE_PASSWORD", ""))
os.environ.setdefault("DB_HOST", os.getenv("DATABASE_HOST", "db"))
os.environ.setdefault("DB_CONN_MAX_AGE", "60")

os.environ.setdefault("ENVIRONMENT", "docker")
os.environ.setdefault("LOG_STDOUT", "yes")
# Runs locally on http (docker-compose): no redirect to https, and a SITE_URL
# with http is fine (see production.py). Set both to "yes" for a docker setup
# behind https.
os.environ.setdefault("SECURE_SSL_REDIRECT", "no")
os.environ.setdefault("SITE_URL_REQUIRE_HTTPS", "no")

# # Strongly suggested to not use this, but explicitly list the allowed hosts. It is
# used to verify if a redirect is safe or not (open redirect vulnerabilities etc.)
# os.environ.setdefault("ALLOWED_HOSTS", "*")

from .production import *  # noqa isort:skip
