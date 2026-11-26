"""
Helpers for the QA tests of phase 2: a guest goes through the checkout with the
test client like a browser does (the total shown in step 3 is posted back as
``expected_total``), pays at the fake provider and comes back.
"""
import re
from urllib.parse import urlencode

from django.urls import reverse

from .utils import commit_hooks, path_of

SITE_URL = "https://booking.example.com"

# The hidden field of step 3 with the total the guest saw.
EXPECTED_TOTAL = re.compile(r'name="confirm-expected_total" value="([^"]*)"')

GUEST = {
    "first_name": "Alex",
    "last_name": "de Landgraaf",
    "email": "alex@example.com",
    "address": "Keizersgracht 117",
    "zipcode": "1015 CJ",
    "city": "Amsterdam",
    "country": "NL",
}

# Complete seller details: the invoices are real, without the watermark.
SELLER = {
    "INVOICE_SELLER_NAME": "Maykin Booking B.V.",
    "INVOICE_SELLER_ADDRESS": "Kingsfordweg 151\\n1043 GR Amsterdam",
    "INVOICE_SELLER_KVK": "12345678",
    "INVOICE_SELLER_VAT_ID": "NL123456789B01",
}


def guest(email, **fields):
    return {**GUEST, "email": email, **fields}


class CheckoutDriver:
    """Mixin for a ``TestCase``: the checkout, the fake provider, the return."""

    def order_url(self, hotel, start, end):
        query = urlencode({"van": start.isoformat(), "tot": end.isoformat()})
        return f"{reverse('order', args=[hotel.pk])}?{query}"

    def post_step(self, url, step, data, *, client=None):
        client = client or self.client
        payload = {"checkout-current_step": step}
        payload.update({f"{step}-{key}": value for key, value in data.items()})
        return client.post(url, payload)

    def stay_step(self, room, start, end, *, code="", client=None):
        """Step 1; returns ``(url, response)``."""
        client = client or self.client
        url = self.order_url(room.hotel, start, end)
        self.assertEqual(client.get(url).status_code, 200)
        response = self.post_step(
            url,
            "stay",
            {
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "room": room.pk,
                "discount_code": code,
            },
            client=client,
        )
        self.assertEqual(response.status_code, 200)
        return url, response

    def to_confirm(self, room, start, end, *, code="", guest_data=None, client=None):
        """
        Steps 1 and 2. Returns ``(url, response of step 3, the total shown)``.
        """
        client = client or self.client
        url, response = self.stay_step(room, start, end, code=code, client=client)
        self.assertEqual(
            response.context["wizard"]["steps"].current,
            "guest",
            response.context["form"].errors,
        )
        response = self.post_step(url, "guest", guest_data or GUEST, client=client)
        self.assertEqual(
            response.context["wizard"]["steps"].current,
            "confirm",
            response.context["form"].errors,
        )
        match = EXPECTED_TOTAL.search(response.content.decode())
        self.assertIsNotNone(match, "step 3 has no expected total")
        return url, response, match.group(1)

    def confirm(self, url, expected_total, *, payment_method="at_hotel", client=None):
        """Step 3; the mails of the commit are sent."""
        client = client or self.client
        with commit_hooks():
            return self.post_step(
                url,
                "confirm",
                {
                    "terms": "on",
                    "payment_method": payment_method,
                    "expected_total": expected_total,
                },
                client=client,
            )

    def book_at_hotel(self, room, start, end, *, code="", guest_data=None, client=None):
        """The whole checkout, paying at the hotel; returns the response."""
        url, _response, total = self.to_confirm(
            room, start, end, code=code, guest_data=guest_data, client=client
        )
        return self.confirm(url, total, client=client)

    def start_online(self, room, start, end, *, code="", guest_data=None, client=None):
        """
        The checkout with online payment, up to the payment page of the fake
        provider. Returns the path of that page.
        """
        url, response, total = self.to_confirm(
            room, start, end, code=code, guest_data=guest_data, client=client
        )
        self.assertContains(response, "Nu online betalen (iDEAL)")
        response = self.confirm(url, total, payment_method="online", client=client)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response["Location"].startswith(SITE_URL + "/nep-betaling/"),
            response["Location"],
        )
        return path_of(response["Location"])

    def pay_and_return(self, payment_page, *, action="paid", client=None):
        """Pay (or not) at the fake provider and come back; returns the response."""
        client = client or self.client
        response = client.post(payment_page, {"action": action})
        self.assertEqual(response.status_code, 302)
        with commit_hooks():
            return client.get(path_of(response["Location"]))
