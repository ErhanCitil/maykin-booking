"""
Where the invoice PDFs are kept: ``PRIVATE_MEDIA_ROOT/invoices``, outside
``MEDIA_ROOT``, so no web server or ``static()`` route ever serves them.

Invoices contain personal data and are only handed out by views that check who
asks (the guest with the token of the order, staff with ``billing.view_invoice``),
so the storage has no URL either.
"""
import os

from django.conf import settings
from django.core.files.storage import FileSystemStorage

INVOICE_DIRECTORY = "invoices"


class InvoiceStorage(FileSystemStorage):
    """
    ``FileSystemStorage`` in ``PRIVATE_MEDIA_ROOT/invoices``.

    The location is computed on every access instead of once, so
    ``override_settings(PRIVATE_MEDIA_ROOT=...)`` in tests moves it as well.
    """

    @property
    def base_location(self):
        return os.path.join(settings.PRIVATE_MEDIA_ROOT, INVOICE_DIRECTORY)

    @property
    def location(self):
        return os.path.abspath(self.base_location)

    def url(self, name):
        raise ValueError(
            "Invoice PDFs have no public URL; serve them through a view that "
            "checks access."
        )


# Only the user the site runs as reads and writes the invoices.
invoice_storage = InvoiceStorage(
    file_permissions_mode=0o600, directory_permissions_mode=0o700
)


def get_invoice_storage():
    """
    The storage of ``Invoice.pdf``. Migrations refer to this function instead of
    to the storage, so they contain an import path and no absolute path.
    """
    return invoice_storage
