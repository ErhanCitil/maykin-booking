from django.test import TestCase

from maykinbooking.accounts.tests.factories import SuperUserFactory
from maykinbooking.base.models import City, Hotel, Review
from maykinbooking.base.tests.factories import CityFactory, HotelFactory, OrderFactory


class ReadOnlyApiTests(TestCase):
    """The API only reads; changes go through the staff dashboard."""

    @classmethod
    def setUpTestData(cls):
        cls.city = CityFactory(name="Amsterdam")
        cls.hotel = HotelFactory(city=cls.city, name="Ibis")
        OrderFactory(hotel=cls.hotel, room__hotel=cls.hotel)
        cls.review = Review.objects.create(
            hotel=cls.hotel, name="Sam", rating=4, description="Prima"
        )
        cls.superuser = SuperUserFactory()

    def detail_urls(self):
        return [
            f"/api/city/{self.city.pk}/",
            f"/api/hotel/{self.hotel.pk}/",
            f"/api/review/{self.review.pk}/",
        ]

    def test_anyone_can_read(self):
        for url in ["/api/city/", "/api/hotel/", "/api/review/", "/api-hotel/"]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)
        for url in self.detail_urls():
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_nobody_can_create_change_or_delete(self):
        self.client.force_login(self.superuser)
        for url in ["/api/city/", "/api/hotel/", "/api/review/"]:
            with self.subTest(url=url, method="post"):
                response = self.client.post(url, {"name": "Nieuw"})
                self.assertEqual(response.status_code, 405)
        for url in self.detail_urls():
            for method in ["put", "patch", "delete"]:
                with self.subTest(url=url, method=method):
                    response = getattr(self.client, method)(
                        url, {"name": "Anders"}, content_type="application/json"
                    )
                    self.assertEqual(response.status_code, 405)

        self.assertEqual(City.objects.get().name, "Amsterdam")
        # A hotel with a booking used to give a 500 on DELETE (ProtectedError).
        self.assertEqual(Hotel.objects.get(pk=self.hotel.pk).name, "Ibis")
        self.assertTrue(Review.objects.filter(pk=self.review.pk).exists())
