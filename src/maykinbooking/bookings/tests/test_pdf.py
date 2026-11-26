"""Review A1: the booking PDF never fetches files or URLs."""
from decimal import Decimal
from unittest import mock

from django.test import TestCase

import weasyprint

from maykinbooking.base.tests.factories import HotelFactory, OrderFactory, RoomFactory

from ..pdf import data_url_fetcher, render_order_pdf

PIXEL = (
    "data:image/gif;base64," "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)


class DataUrlFetcherTests(TestCase):
    def test_only_data_urls(self):
        result = data_url_fetcher(PIXEL)
        self.assertEqual(result["mime_type"], "image/gif")
        for url in [
            "file:///etc/passwd",
            "http://169.254.169.254/latest/meta-data/",
            "https://fonts.example.com/font.woff2",
            "//example.com/style.css",
            "/static/css/screen.css",
        ]:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    data_url_fetcher(url)

    def test_external_resources_are_left_out_of_a_document(self):
        html = (
            '<link rel="stylesheet" href="file:///etc/passwd">'
            '<img src="http://169.254.169.254/x.png">'
            f'<img src="{PIXEL}">'
        )
        with mock.patch(
            "maykinbooking.bookings.pdf.weasyprint.default_url_fetcher",
            wraps=weasyprint.default_url_fetcher,
        ) as fetch, self.assertLogs("weasyprint", "ERROR"):
            pdf = weasyprint.HTML(string=html, url_fetcher=data_url_fetcher).write_pdf()
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertEqual([call.args[0] for call in fetch.call_args_list], [PIXEL])


class RenderOrderPdfTests(TestCase):
    def test_renders_with_the_data_url_fetcher(self):
        room = RoomFactory(hotel=HotelFactory(), price=Decimal("100"))
        order = OrderFactory(room=room, hotel=room.hotel)
        with mock.patch(
            "maykinbooking.bookings.pdf.weasyprint.HTML", wraps=weasyprint.HTML
        ) as html:
            pdf = render_order_pdf(order)
        self.assertIs(html.call_args.kwargs["url_fetcher"], data_url_fetcher)
        self.assertTrue(pdf.startswith(b"%PDF"))
