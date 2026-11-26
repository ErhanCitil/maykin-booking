import logging
import re

from decouple import Csv, config as _config, undefined
from sentry_sdk.integrations import DidNotEnable, django, redis

logger = logging.getLogger(__name__)


def config(option: str, default=undefined, *args, **kwargs):
    """
    Pull a config parameter from the environment.

    Read the config variable ``option``. If it's optional, use the ``default`` value.
    Input is automatically cast to the correct type, where the type is derived from the
    default value if possible.

    Pass ``split=True`` to split the comma-separated input into a list.
    """
    if "split" in kwargs:
        kwargs.pop("split")
        kwargs["cast"] = Csv()
        if default == []:
            default = ""

    if default is not undefined and default is not None:
        kwargs.setdefault("cast", type(default))
    return _config(option, default=default, *args, **kwargs)


# The token of "Mijn boeking" and the reference of a payment (its return URL)
# work as passwords in a URL: keep them out of Sentry.
BOOKING_TOKEN_IN_URL = re.compile(
    r"/(boeking|betalen)/[0-9a-fA-F]{8}-?(?:[0-9a-fA-F]{4}-?){3}[0-9a-fA-F]{12}"
)


def _scrub_booking_tokens(value):
    if isinstance(value, str):
        return BOOKING_TOKEN_IN_URL.sub(r"/\1/<token>", value)
    if isinstance(value, dict):
        return {key: _scrub_booking_tokens(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_scrub_booking_tokens(item) for item in value)
    return value


def scrub_booking_tokens(event, hint):
    """
    Sentry ``before_send``: replace ``/boeking/<uuid>`` by ``/boeking/<token>``
    and ``/betalen/<uuid>`` by ``/betalen/<token>`` everywhere in the event
    (request URL, headers such as the referrer, breadcrumbs, messages).
    """
    return _scrub_booking_tokens(event)


def get_sentry_options() -> dict:
    """
    Privacy options for ``sentry_sdk.init``: no personal data of guests (IP
    addresses, cookies, users), no local variables (a view has the booking token
    in its frame), no request bodies (the guest details of the checkout) and no
    booking tokens in URLs.
    """
    return {
        "send_default_pii": False,
        "include_local_variables": False,
        # Called ``request_bodies`` before; sentry-sdk 1.31 still accepts that
        # name, but deprecated.
        "max_request_body_size": "never",
        "before_send": scrub_booking_tokens,
    }


def get_sentry_integrations() -> list:
    """
    Determine which Sentry SDK integrations to enable.
    """
    default = [
        django.DjangoIntegration(),
        redis.RedisIntegration(),
    ]
    extra = []

    try:
        from sentry_sdk.integrations import celery
    except DidNotEnable:  # happens if the celery import fails by the integration
        pass
    else:
        extra.append(celery.CeleryIntegration())

    return [*default, *extra]
