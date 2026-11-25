from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from ..models import City, Hotel, Order, Room


class CheckoutTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        city = City.objects.create(city_id="AMS", name="Amsterdam")
        cls.hotel = Hotel.objects.create(
            city=city, hotel_id="AMS01", name="Ibis Amsterdam"
        )
        cls.room = Room.objects.create(
            hotel=cls.hotel,
            room_type="Double",
            price=Decimal("100"),
            description="Kamer",
        )
        cls.url = reverse("order", args=[cls.hotel.pk])
        cls.start = date.today() + timedelta(days=7)
        cls.end = cls.start + timedelta(days=3)

    def post_step(self, step, data):
        payload = {"checkout-current_step": step}
        payload.update({f"{step}-{key}": value for key, value in data.items()})
        return self.client.post(self.url, payload)

    def post_stay(self, **overrides):
        data = {"start_date": self.start, "end_date": self.end, "room": self.room.pk}
        data.update(overrides)
        return self.post_step("stay", data)

    def post_guest(self):
        return self.post_step(
            "guest",
            {
                "first_name": "Alex",
                "last_name": "de Landgraaf",
                "email": "alex@example.com",
                "address": "Keizersgracht 117",
                "zipcode": "1015 CJ",
                "country": "NL",
            },
        )

    def test_first_step_lists_rooms(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Kies je kamer")
        self.assertContains(response, "Double")

    def test_unknown_hotel_is_404(self):
        response = self.client.get(reverse("order", args=[999999]))
        self.assertEqual(response.status_code, 404)

    def test_full_checkout_creates_one_order(self):
        self.client.get(self.url)
        response = self.post_stay()
        self.assertContains(response, "Wie verblijft er?")
        # The summary shows the price breakdown after step 1.
        self.assertContains(response, "× 3 nachten")
        self.assertContains(response, "€ 300,00")

        response = self.post_guest()
        self.assertContains(response, "Klopt alles?")
        self.assertContains(response, "alex@example.com")
        self.assertEqual(Order.objects.count(), 0)

        response = self.post_step("confirm", {"terms": "on"})
        order = Order.objects.get()
        self.assertRedirects(response, reverse("success", args=[order.pk]))
        self.assertEqual(order.hotel, self.hotel)
        self.assertEqual(order.room, self.room)
        self.assertEqual(order.start_date, self.start)
        self.assertEqual(order.end_date, self.end)
        self.assertEqual(order.zipcode, "1015CJ")
        self.assertEqual(order.total_price, Decimal("300"))

        response = self.client.get(reverse("success", args=[order.pk]))
        self.assertContains(response, "Je boeking staat vast")
        self.assertContains(response, "€ 300,00")

    def test_terms_must_be_accepted(self):
        self.client.get(self.url)
        self.post_stay()
        self.post_guest()
        response = self.post_step("confirm", {})
        self.assertContains(response, "Je moet akkoord gaan met de voorwaarden")
        self.assertEqual(Order.objects.count(), 0)

    def test_invalid_dates_stay_on_first_step(self):
        self.client.get(self.url)
        response = self.post_stay(end_date=self.start)
        self.assertContains(response, "Uitchecken moet na de incheckdatum zijn.")
        self.assertContains(response, "Kies je kamer")

    def test_room_that_became_unavailable_is_caught_on_confirm(self):
        self.client.get(self.url)
        self.post_stay()
        self.post_guest()
        Room.objects.filter(pk=self.room.pk).update(is_available=False)
        response = self.post_step("confirm", {"terms": "on"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Order.objects.count(), 0)

    def test_hotel_without_available_rooms_cannot_be_booked(self):
        Room.objects.filter(pk=self.room.pk).update(is_available=False)
        response = self.client.get(self.url)
        self.assertContains(response, "niet te boeken")
        self.post_stay()
        self.assertEqual(Order.objects.count(), 0)


class GuestOrderAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        city = City.objects.create(city_id="AMS", name="Amsterdam")
        hotel = Hotel.objects.create(city=city, hotel_id="AMS01", name="Ibis")
        room = Room.objects.create(
            hotel=hotel, room_type="Double", price=Decimal("100"), description="x"
        )
        cls.order = Order.objects.create(
            hotel=hotel,
            room=room,
            first_name="Alex",
            last_name="L",
            start_date=date(2027, 1, 1),
            end_date=date(2027, 1, 3),
        )

    def test_success_page_without_booking_in_session_is_404(self):
        response = self.client.get(reverse("success", args=[self.order.pk]))
        self.assertEqual(response.status_code, 404)

    def test_pdf_without_booking_in_session_is_404(self):
        response = self.client.get(reverse("orderpdf", args=[self.order.pk]))
        self.assertEqual(response.status_code, 404)

    def test_guest_can_see_own_booking(self):
        session = self.client.session
        session["order_token"] = str(self.order.token)
        session.save()
        response = self.client.get(reverse("success", args=[self.order.pk]))
        self.assertContains(response, "Boekingsnummer #{}".format(self.order.pk))
        self.assertContains(response, "× 2 nachten")

    def test_guest_can_download_pdf(self):
        session = self.client.session
        session["order_token"] = str(self.order.token)
        session.save()
        response = self.client.get(reverse("orderpdf", args=[self.order.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn(
            "boeking_{}.pdf".format(self.order.pk), response["Content-Disposition"]
        )
