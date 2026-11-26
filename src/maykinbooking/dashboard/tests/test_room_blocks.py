"""Blocking rooms of a type for a while, e.g. for maintenance."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format

from maykinbooking.accounts.tests.factories import ManagerUserFactory, UserFactory
from maykinbooking.base.models import RoomBlock
from maykinbooking.base.tests.factories import (
    HotelFactory,
    OrderFactory,
    RoomBlockFactory,
    RoomFactory,
)
from maykinbooking.bookings.services.availability import free_counts


class RoomBlockTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = ManagerUserFactory(username="medewerker")
        cls.user = UserFactory()
        cls.hotel = HotelFactory(name="Hotel aan de Gracht", is_available=True)
        cls.room = RoomFactory(
            hotel=cls.hotel, room_type="Double", price=Decimal("100"), inventory=2
        )
        cls.other_room = RoomFactory(hotel=cls.hotel, room_type="Single")

    def setUp(self):
        self.today = timezone.localdate()
        self.url = reverse("dashboard:room_blocks", args=[self.room.pk])
        self.client.force_login(self.staff)

    def day(self, offset):
        return self.today + timedelta(days=offset)

    def block_data(self, start=7, end=9, quantity=1, reason="Lekkage"):
        return {
            "start_date": self.day(start).isoformat(),
            "end_date": self.day(end).isoformat(),
            "quantity": str(quantity),
            "reason": reason,
        }

    def messages(self, response):
        return [(m.level_tag, str(m)) for m in get_messages(response.wsgi_request)]


class AccessTests(RoomBlockTestCase):
    def setUp(self):
        super().setUp()
        self.client.logout()
        self.block = RoomBlockFactory(room=self.room)
        self.delete_url = reverse("dashboard:room_block_delete", args=[self.block.pk])

    def test_anonymous_users_are_sent_to_the_login_page(self):
        for method, url in (
            ("get", self.url),
            ("post", self.url),
            ("post", self.delete_url),
        ):
            with self.subTest(method=method, url=url):
                response = getattr(self.client, method)(url)
                self.assertRedirects(
                    response,
                    f"{reverse('login')}?next={url}",
                    fetch_redirect_response=False,
                )

    def test_users_without_staff_status_get_403(self):
        self.client.force_login(self.user)

        self.assertEqual(self.client.get(self.url).status_code, 403)
        response = self.client.post(self.url, self.block_data(start=20, end=22))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.post(self.delete_url).status_code, 403)

        self.assertEqual(list(RoomBlock.objects.all()), [self.block])


class RoomBlockListTests(RoomBlockTestCase):
    def test_lists_the_current_and_planned_blocks_of_this_room(self):
        RoomBlockFactory(
            room=self.room, start_date=self.day(-1), end_date=self.day(2), reason="Nu"
        )
        RoomBlockFactory(
            room=self.room,
            start_date=self.day(30),
            end_date=self.day(32),
            reason="Later",
        )
        RoomBlockFactory(
            room=self.room,
            start_date=self.day(-9),
            end_date=self.day(-2),
            reason="Voorbij",
        )
        RoomBlockFactory(room=self.other_room, reason="Andere kamer")

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [block.reason for block in response.context["blocks"]], ["Nu", "Later"]
        )
        self.assertNotContains(response, "Voorbij")
        self.assertNotContains(response, "Andere kamer")
        self.assertContains(response, "2 kamers van dit type")

    def test_delete_asks_for_confirmation(self):
        block = RoomBlockFactory(room=self.room)

        response = self.client.get(self.url)

        delete_url = reverse("dashboard:room_block_delete", args=[block.pk])
        self.assertContains(response, f'action="{delete_url}"')
        self.assertContains(response, "<details>")
        self.assertContains(response, "Ja, verwijderen")

    def test_room_and_hotel_pages_link_to_the_blocks(self):
        for url in (
            reverse("dashboard:room_edit", args=[self.room.pk]),
            reverse("dashboard:hotel_edit", args=[self.hotel.pk]),
        ):
            with self.subTest(url=url):
                self.assertContains(self.client.get(url), f'href="{self.url}"')

    def test_unknown_room_is_404(self):
        response = self.client.get(reverse("dashboard:room_blocks", args=[999999]))
        self.assertEqual(response.status_code, 404)


class CreateRoomBlockTests(RoomBlockTestCase):
    def test_creates_the_block(self):
        response = self.client.post(self.url, self.block_data(quantity=2))

        self.assertRedirects(response, self.url)
        block = RoomBlock.objects.get()
        self.assertEqual(block.room, self.room)
        self.assertEqual((block.start_date, block.end_date), (self.day(7), self.day(9)))
        self.assertEqual(block.quantity, 2)
        self.assertEqual(block.reason, "Lekkage")
        self.assertEqual(block.created_by, self.staff)
        ((level, message),) = self.messages(response)
        self.assertEqual(level, "success")
        self.assertIn("2 kamers zijn geblokkeerd", message)

        # The blocked rooms cannot be booked anymore.
        self.assertEqual(
            free_counts([self.room], self.day(7), self.day(9)), {self.room.pk: 0}
        )
        self.assertEqual(
            free_counts([self.room], self.day(9), self.day(10)), {self.room.pk: 2}
        )

    def test_reason_is_optional(self):
        self.client.post(self.url, self.block_data(reason=""))
        self.assertEqual(RoomBlock.objects.get().reason, "")

    def test_warns_about_overbooked_nights_and_still_blocks(self):
        # Two bookings on day 8, so blocking one of the two rooms overbooks it.
        for _ in range(2):
            OrderFactory(
                hotel=self.hotel,
                room=self.room,
                start_date=self.day(8),
                end_date=self.day(9),
            )

        response = self.client.post(self.url, self.block_data(start=7, end=10))

        self.assertRedirects(response, self.url)
        self.assertEqual(RoomBlock.objects.count(), 1)
        levels = dict(self.messages(response))
        self.assertIn(
            f"op 1 nacht zijn meer kamers bezet dan de 2 kamers van dit type: "
            f"{date_format(self.day(8), 'j b Y')} (3)",
            levels["warning"],
        )

    def test_end_must_be_after_start(self):
        for start, end in ((9, 9), (9, 7)):
            with self.subTest(start=start, end=end):
                response = self.client.post(
                    self.url, self.block_data(start=start, end=end)
                )
                self.assertEqual(response.status_code, 200)
                self.assertIn("end_date", response.context["form"].errors)
        self.assertFalse(RoomBlock.objects.exists())

    def test_quantity_must_be_at_least_one(self):
        response = self.client.post(self.url, self.block_data(quantity=0))

        self.assertEqual(response.status_code, 200)
        self.assertIn("quantity", response.context["form"].errors)
        self.assertFalse(RoomBlock.objects.exists())

    def test_dates_are_required(self):
        response = self.client.post(self.url, {"quantity": "1"})

        form = response.context["form"]
        self.assertIn("start_date", form.errors)
        self.assertIn("end_date", form.errors)
        self.assertFalse(RoomBlock.objects.exists())


class DeleteRoomBlockTests(RoomBlockTestCase):
    def test_deletes_the_block(self):
        block = RoomBlockFactory(room=self.room)

        response = self.client.post(
            reverse("dashboard:room_block_delete", args=[block.pk])
        )

        self.assertRedirects(response, self.url)
        self.assertFalse(RoomBlock.objects.exists())
        ((level, message),) = self.messages(response)
        self.assertEqual(level, "success")
        self.assertIn("is verwijderd", message)

    def test_get_does_not_delete(self):
        block = RoomBlockFactory(room=self.room)

        response = self.client.get(
            reverse("dashboard:room_block_delete", args=[block.pk])
        )

        self.assertEqual(response.status_code, 405)
        self.assertTrue(RoomBlock.objects.filter(pk=block.pk).exists())

    def test_unknown_block_is_404(self):
        response = self.client.post(
            reverse("dashboard:room_block_delete", args=[999999])
        )
        self.assertEqual(response.status_code, 404)
