from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from maykinbooking.base.models import Order

from ..validators import no_control_characters


class NoControlCharactersTests(SimpleTestCase):
    def test_refused(self):
        cases = {
            "CR LF (Cc)": "Alex\r\nBcc: x@example.com",
            "tab (Cc)": "Alex\tde Landgraaf",
            "NUL (Cc)": "Alex\x00",
            "line separator (Zl)": "Alex\u2028de Landgraaf",
            "paragraph separator (Zp)": "Alex\u2029de Landgraaf",
            "left-to-right embedding": "\u202aAlex",
            "pop directional formatting": "Alex\u202c",
            "right-to-left override": "Alex\u202eexe.pdf",
            "left-to-right isolate": "\u2066Alex",
            "pop directional isolate": "Alex\u2069",
        }
        for case, value in cases.items():
            with self.subTest(case):
                with self.assertRaises(ValidationError) as error:
                    no_control_characters(value)
                self.assertEqual(error.exception.code, "control_characters")
                self.assertEqual(
                    error.exception.messages,
                    ["Gebruik geen regeleinden of speciale tekens."],
                )

    def test_allowed(self):
        cases = {
            "empty": "",
            "plain": "Alex de Landgraaf",
            "accents": "Zo\u00eb-Anne d'Hondt",
            "Danish": "\u00c6r\u00f8 3, bus 2",
            "zipcode": "1015CJ",
            "Polish": "\u0141\u00f3d\u017a",
            "Greek": "\u0395\u03bb\u03ad\u03bd\u03b7",
            "Chinese": "\u674e\u5c0f\u9f8d",
            # Zero width non-joiner and joiner: needed in Persian and Indian names.
            "ZWNJ (Persian)": "Mohammad\u200cReza",
            "ZWJ (Devanagari)": "\u0915\u094d\u200d\u0937",
            # Other format characters that do not turn text around.
            "left-to-right mark": "Alex\u200eLandgraaf",
            "zero width space": "Al\u200bex",
        }
        for case, value in cases.items():
            with self.subTest(case):
                no_control_characters(value)

    def test_on_the_guest_fields_of_an_order(self):
        for name in ["first_name", "last_name", "address", "zipcode"]:
            with self.subTest(field=name):
                field = Order._meta.get_field(name)
                self.assertIn(no_control_characters, field.validators)
