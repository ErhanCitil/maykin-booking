"""
The booking confirmation as PDF.

One rendering path for the download on the "Mijn boeking" page and the
attachment of the confirmation mail.
"""
from django.template.loader import render_to_string

import weasyprint


def data_url_fetcher(url, timeout=10, ssl_context=None):
    """
    WeasyPrint URL fetcher that only allows inline ``data:`` URLs.

    The PDF contains text from guests and staff (names, hotel descriptions); it
    must never make WeasyPrint read local files or call other servers. A refused
    resource is left out of the PDF and logged by WeasyPrint as a warning.
    """
    if not url.startswith("data:"):
        raise ValueError(f"Only data: URLs are allowed in the booking PDF: {url!r}")
    return weasyprint.default_url_fetcher(url, timeout=timeout, ssl_context=ssl_context)


def render_order_pdf(order):
    """Render ``order_pdf.html`` for ``order`` and return the PDF as bytes."""
    # Imported here because the emails module imports this one.
    from .emails import booking_context

    html = render_to_string("order_pdf.html", booking_context(order))
    return weasyprint.HTML(string=html, url_fetcher=data_url_fetcher).write_pdf()
