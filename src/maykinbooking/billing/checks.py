"""
System checks of billing.
"""
from pathlib import Path

from django.conf import settings
from django.core.checks import Error, register


@register()
def check_private_media_root(app_configs, **kwargs):
    """
    ``PRIVATE_MEDIA_ROOT`` (the invoice PDFs) must not be in ``MEDIA_ROOT`` or
    ``STATIC_ROOT``: those are served as files, without any access check.
    """
    private = Path(settings.PRIVATE_MEDIA_ROOT).resolve()
    errors = []
    for name in ("MEDIA_ROOT", "STATIC_ROOT"):
        value = getattr(settings, name, None)
        if not value:
            continue
        public = Path(value).resolve()
        if private == public or public in private.parents:
            errors.append(
                Error(
                    f"PRIVATE_MEDIA_ROOT ({private}) is inside {name} ({public}).",
                    hint=(
                        f"Invoices in {name} could be downloaded by anyone. Choose "
                        "a directory outside the media and static files."
                    ),
                    id="billing.E001",
                )
            )
    return errors
