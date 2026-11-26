"""
QA, phase 2 "Geld": regression tests for the problems found in this round.

* IDOR on "Mijn boeking": the token decides the order, never a ``pk`` in the
  URL (the number of an invoice). Tested on the view itself, so it does not
  depend on order and invoice ids being equal (they are on SQLite, where the
  ids start again in every test, but not on PostgreSQL).
* A price that drops to 0 while a guest confirms online: with the total the
  guest saw (``expected_total``) the new price is shown; without it the guest
  must not get a server error either.
"""
from datetime import date
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import Client, RequestFactory, TestCase, override_settings

from freezegun import freeze_time

from maykinbooking.base import views as base_views
from maykinbooking.base.models import City, Hotel, Order, Room
from maykinbooking.bookings.views import BookingInvoiceView

from ..models import DiscountCode, Payment
from .factories import DiscountCodeFactory
from .money_flows import SITE_URL, CheckoutDriver
from .utils import TempMediaMixin, book

D = Decimal


@override_settings(SITE_URL=SITE_URL, MOLLIE_API_KEY="", PAYMENT_FAKE_PROVIDER=True)
class RegressionTestCase(CheckoutDriver, TempMediaMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        city = City.objects.create(city_id="AMS", name="Amsterdam")
        cls.hotel = Hotel.objects.create(
            city=city, hotel_id="AMS01", name="Hotel de Gracht", is_available=True
        )
        cls.room = Room.objects.create(
            hotel=cls.hotel,
            room_type="Double",
            price=D("100.00"),
            description="Kamer",
            inventory=3,
        )

    def setUp(self):
        cache.clear()


@freeze_time("2027-03-01T09:00:00Z")
class InvoiceTokenRegressionTests(RegressionTestCase):
    def test_the_token_decides_the_order_whatever_the_pk_in_the_url(self):
        mine = book(self.room, date(2027, 3, 10), date(2027, 3, 12))
        other = book(self.room, date(2027, 3, 20), date(2027, 3, 21))
        view = BookingInvoiceView()
        # The pk in the URL is the pk of the *other* order: before the fix
        # SingleObjectMixin looked the order up by that pk.
        view.setup(RequestFactory().get("/"), token=mine.token, pk=other.pk)
        self.assertEqual(view.get_object(), mine)

        view.setup(RequestFactory().get("/"), token=other.token, pk=mine.pk)
        self.assertEqual(view.get_object(), other)


@freeze_time("2027-03-01T09:00:00Z")
class PriceDropsToZeroWhileConfirmingTests(RegressionTestCase):
    """
    Staff changes an unused code from 50% to 100% between the validation of
    step 3 and ``create_booking`` (a race of milliseconds, simulated by
    changing the code just before ``create_booking`` runs).
    """

    def setUp(self):
        super().setUp()
        self.code = DiscountCodeFactory(code="HALF", value=D("50"))
        self.url, response, self.total = self.to_confirm(
            self.room, date(2027, 3, 10), date(2027, 3, 12), code="half"
        )
        self.assertEqual(self.total, "100.00")
        self.assertContains(response, "Nu online betalen (iDEAL)")

    def code_changes_meanwhile(self):
        book_room = base_views.book_room

        def change_the_code_first(**kwargs):
            DiscountCode.objects.filter(pk=self.code.pk).update(value=D("100"))
            return book_room(**kwargs)

        return mock.patch.object(
            base_views, "book_room", side_effect=change_the_code_first
        )

    def test_with_the_total_the_guest_saw_the_new_price_is_shown(self):
        with self.code_changes_meanwhile():
            response = self.confirm(self.url, self.total, payment_method="online")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "De prijs van dit verblijf is gewijzigd.")
        self.assertFalse(Order.objects.exists())

    def test_without_the_total_the_guest_gets_no_server_error(self):
        """
        Was a bug (QA B1): ``create_booking`` raised ``ValueError("Online
        payment needs a total above 0.")``, which the checkout did not catch:
        HTTP 500. Now ``OnlinePaymentUnavailable``: step 3 again with a message
        and the new price, and nothing booked.
        """
        client = Client(raise_request_exception=False)
        url, _response, _total = self.to_confirm(
            self.room, date(2027, 3, 10), date(2027, 3, 12), code="half", client=client
        )
        with self.code_changes_meanwhile():
            # A post without the hidden expected total (a page from before
            # the field existed, or a script).
            response = self.post_step(
                url,
                "confirm",
                {"terms": "on", "payment_method": "online"},
                client=client,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["wizard"]["steps"].current, "confirm")
        self.assertContains(response, "Er valt niets online te betalen")
        self.assertContains(response, "Er is niets geboekt.")
        self.assertFalse(Order.objects.exists())
        self.assertFalse(Payment.objects.exists())
        # The new price is shown, and online payment is no longer offered.
        self.assertContains(response, 'name="confirm-expected_total" value="0.00"')
        self.assertNotContains(response, "Nu online betalen (iDEAL)")
