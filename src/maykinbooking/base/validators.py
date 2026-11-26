import unicodedata

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

# Control characters (Cc, e.g. line breaks and tabs) and line and paragraph
# separators (Zl, Zp).
FORBIDDEN_CATEGORIES = frozenset({"Cc", "Zl", "Zp"})

# The bidirectional embeddings and overrides (U+202A-U+202E) and isolates
# (U+2066-U+2069), which can turn text around ("exe.pdf" shown as "fdp.exe").
# Other format characters (Cf) are allowed: names in e.g. Persian and Indian
# scripts need the zero width (non-)joiner, U+200C and U+200D.
BIDI_CONTROLS = frozenset(
    chr(code) for code in [*range(0x202A, 0x202F), *range(0x2066, 0x206A)]
)


def no_control_characters(value):
    """
    Reject text with characters that do not belong in a single-line field.

    Guest details end up in mail headers and bodies, PDFs and CSV exports; a line
    break or a direction override there can change what a reader sees.
    """
    if any(
        char in BIDI_CONTROLS or unicodedata.category(char) in FORBIDDEN_CATEGORIES
        for char in value
    ):
        raise ValidationError(
            _("Gebruik geen regeleinden of speciale tekens."),
            code="control_characters",
        )
