from datetime import date
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import TestCase

from maykinbooking.base.models import (
    EmailLog,
    Hotel,
    Order,
    OrderNight,
    OrderStatusChange,
    Room,
    get_upload_storage,
    upload_storage,
)
from maykinbooking.base.tests import factories


class TestOrder(TestCase):
    def setUp(self):
        self.hotel = factories.HotelFactory()
        self.room = factories.RoomFactory(
            hotel=self.hotel, room_type="Single", price=100.00
        )
        self.order = factories.OrderFactory(
            room=self.room,
            hotel=self.hotel,
            start_date=date(2023, 1, 1),
            end_date=date(2023, 1, 3),
        )

    def test_order_total_price(self):
        self.order.refresh_from_db()
        self.assertEqual(self.order.total_price, Decimal("200.00"))

    def test_night_count(self):
        self.assertEqual(self.order.night_count, 2)

    def test_factory_makes_a_night_per_night_at_the_room_price(self):
        self.assertEqual(
            list(self.order.nights.values_list("date", "price")),
            [
                (date(2023, 1, 1), Decimal("100.00")),
                (date(2023, 1, 2), Decimal("100.00")),
            ],
        )

    def test_factory_defaults_to_confirmed_and_payment_at_the_hotel(self):
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "confirmed")
        self.assertEqual(self.order.payment_status, "at_hotel")
        self.assertEqual(self.order.language, "nl")

    def test_total_price_is_a_snapshot(self):
        Room.objects.filter(pk=self.room.pk).update(price=Decimal("150"))
        self.order.refresh_from_db()
        self.assertEqual(self.order.total_price, Decimal("200.00"))
        self.assertEqual(
            set(self.order.nights.values_list("price", flat=True)), {Decimal("100")}
        )

    def test_deleting_an_order_deletes_its_history(self):
        OrderStatusChange.objects.create(
            order=self.order, field="status", new_value="confirmed", actor_type="system"
        )
        EmailLog.objects.create(
            order=self.order, kind="booking_confirmation", recipient="a@example.com"
        )
        self.order.delete()
        self.assertFalse(OrderNight.objects.exists())
        self.assertFalse(OrderStatusChange.objects.exists())
        self.assertFalse(EmailLog.objects.exists())

    def test_hotel_and_room_with_orders_cannot_be_deleted(self):
        with self.assertRaises(ProtectedError):
            self.room.delete()
        with self.assertRaises(ProtectedError):
            self.hotel.delete()
        self.assertTrue(Order.objects.filter(pk=self.order.pk).exists())

    def test_reverse_relations(self):
        self.assertEqual(list(self.hotel.orders.all()), [self.order])
        self.assertEqual(list(self.room.orders.all()), [self.order])


class TestOrderConstraints(TestCase):
    def setUp(self):
        self.room = factories.RoomFactory()

    def create_order(self, **fields):
        values = {
            "room": self.room,
            "hotel": self.room.hotel,
            "start_date": date(2027, 1, 10),
            "end_date": date(2027, 1, 12),
            "total_price": Decimal("100"),
        }
        values.update(fields)
        with transaction.atomic():
            return Order.objects.create(**values)

    def test_valid_order(self):
        self.create_order()

    def test_end_date_must_be_after_start_date(self):
        for end_date in [date(2027, 1, 10), date(2027, 1, 9)]:
            with self.subTest(end_date=end_date):
                with self.assertRaises(IntegrityError):
                    self.create_order(end_date=end_date)

    def test_dates_and_total_price_are_required(self):
        for field in ["start_date", "end_date", "total_price"]:
            with self.subTest(field=field):
                with self.assertRaises(IntegrityError):
                    self.create_order(**{field: None})

    def test_unknown_status_or_payment_status(self):
        for field in ["status", "payment_status"]:
            with self.subTest(field=field):
                with self.assertRaises(IntegrityError):
                    self.create_order(**{field: "onbekend"})

    def test_token_is_unique(self):
        order = self.create_order()
        with self.assertRaises(IntegrityError):
            self.create_order(token=order.token)

    def test_one_night_per_date(self):
        order = self.create_order()
        OrderNight.objects.create(order=order, date=date(2027, 1, 10), price=1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            OrderNight.objects.create(order=order, date=date(2027, 1, 10), price=1)


class TestRoomBlock(TestCase):
    def test_factory(self):
        block = factories.RoomBlockFactory()
        self.assertEqual((block.end_date - block.start_date).days, 2)
        self.assertEqual(list(block.room.blocks.all()), [block])

    def test_end_date_must_be_after_start_date(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            factories.RoomBlockFactory(
                start_date=date(2027, 1, 10), end_date=date(2027, 1, 10)
            )

    def test_quantity_at_least_one(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            factories.RoomBlockFactory(quantity=0)

    def test_blocks_are_deleted_with_their_room(self):
        block = factories.RoomBlockFactory()
        block.room.delete()
        self.assertFalse(type(block).objects.exists())


class TestRoom(TestCase):
    def test_inventory_defaults_to_five(self):
        room = Room.objects.create(
            hotel=factories.HotelFactory(), room_type="Single", description="x"
        )
        room.refresh_from_db()
        self.assertEqual(room.inventory, 5)

    def test_price_up_to_six_digits_before_the_decimal_point(self):
        room = factories.RoomFactory(price=Decimal("123456.78"))
        room.refresh_from_db()
        self.assertEqual(room.price, Decimal("123456.78"))


class TestUploadStorage(TestCase):
    def test_image_fields_use_the_storage_function(self):
        self.assertIs(get_upload_storage(), upload_storage)
        for model in [Hotel, Room]:
            with self.subTest(model=model.__name__):
                field = model._meta.get_field("image")
                self.assertIs(field.storage, upload_storage)
                # Migrations refer to the function, not to a path on this machine.
                _name, _path, _args, kwargs = field.deconstruct()
                self.assertIs(kwargs["storage"], get_upload_storage)
