"""
An invoice or credit note as PDF, rendered from its snapshot fields only.
"""
from django.conf import settings
from django.template.loader import render_to_string
from django.utils import translation

import weasyprint

from maykinbooking.utils.pdf import data_url_fetcher


def render_invoice_pdf(invoice, lines):
    """
    Render ``billing/invoice_pdf.html`` for ``invoice`` with its ``lines`` and
    return the PDF as bytes, in the language of the order.
    """
    language = invoice.order.language or settings.LANGUAGE_CODE
    with translation.override(language):
        html = render_to_string(
            "billing/invoice_pdf.html",
            {
                "invoice": invoice,
                "lines": lines,
                "credited_invoice": invoice.credited_invoice,
                "seller_address_lines": invoice.seller_address.splitlines(),
                "customer_address_lines": invoice.customer_address.splitlines(),
            },
        )
    return weasyprint.HTML(string=html, url_fetcher=data_url_fetcher).write_pdf()
