"""Security S6: the token of "Mijn boeking" and guest data never end up in Sentry."""
from django.test import SimpleTestCase

import sentry_sdk

from maykinbooking.conf.utils import get_sentry_options, scrub_booking_tokens

TOKEN = "0f6b6a5e-3c1d-4b7a-9e2f-8c0d1a2b3c4d"


class ScrubBookingTokensTests(SimpleTestCase):
    def test_tokens_are_replaced_everywhere_in_the_event(self):
        event = {
            "request": {
                "url": f"https://booking.example.com/boeking/{TOKEN}/annuleren/",
                "headers": {
                    "Referer": f"https://booking.example.com/boeking/{TOKEN}/",
                },
            },
            "breadcrumbs": {
                "values": [
                    {"message": f"GET /boeking/{TOKEN.replace('-', '')}/pdf/"},
                    {"data": {"url": f"/boeking/{TOKEN.upper()}/"}},
                ]
            },
            "transaction": "/boeking/<uuid:token>/",
            "level": "error",
            "extra": {"count": 3, "ids": (1, 2)},
        }

        scrubbed = scrub_booking_tokens(event, hint={})

        self.assertNotIn(TOKEN[:8], str(scrubbed).lower())
        self.assertEqual(
            scrubbed["request"]["url"],
            "https://booking.example.com/boeking/<token>/annuleren/",
        )
        self.assertEqual(
            scrubbed["request"]["headers"]["Referer"],
            "https://booking.example.com/boeking/<token>/",
        )
        self.assertEqual(
            scrubbed["breadcrumbs"]["values"][0]["message"], "GET /boeking/<token>/pdf/"
        )
        # Everything else is left alone.
        self.assertEqual(scrubbed["transaction"], "/boeking/<uuid:token>/")
        self.assertEqual(scrubbed["level"], "error")
        self.assertEqual(scrubbed["extra"], {"count": 3, "ids": (1, 2)})

    def test_other_urls_are_kept(self):
        event = {
            "request": {"url": "https://booking.example.com/stad/Amsterdam?page=2"}
        }
        self.assertEqual(scrub_booking_tokens(event, hint={}), event)


class SentryOptionsTests(SimpleTestCase):
    def test_privacy_options(self):
        options = get_sentry_options()
        self.assertIs(options["send_default_pii"], False)
        # A view has the booking token in a local variable.
        self.assertIs(options["include_local_variables"], False)
        # The checkout posts the guest details.
        self.assertEqual(options["max_request_body_size"], "never")
        self.assertIs(options["before_send"], scrub_booking_tokens)

    def test_the_installed_sentry_sdk_knows_the_options(self):
        # An unknown option raises TypeError.
        client = sentry_sdk.Client(**get_sentry_options())
        for name, value in get_sentry_options().items():
            with self.subTest(name):
                self.assertEqual(client.options[name], value)
