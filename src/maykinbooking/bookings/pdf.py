"""
The booking confirmation as PDF.

One rendering path for the download on the "Mijn boeking" page and the
attachment of the confirmation mail.
"""
from django.template.loader import render_to_string

import weasyprint

from maykinbooking.utils.pdf import data_url_fetcher

__all__ = ["data_url_fetcher", "render_order_pdf"]


def render_order_pdf(order):
    """Render ``order_pdf.html`` for ``order`` and return the PDF as bytes."""
    # Imported here because the emails module imports this one.
    from .emails import booking_context

    html = render_to_string("order_pdf.html", booking_context(order))
    return weasyprint.HTML(string=html, url_fetcher=data_url_fetcher).write_pdf()
