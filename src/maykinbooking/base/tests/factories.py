import datetime
from decimal import Decimal

from django.utils import timezone

import factory
import factory.fuzzy

from maykinbooking.base.models import (
    City,
    Highlight,
    Hotel,
    Order,
    OrderNight,
    OrderStatus,
    PaymentStatus,
    Room,
    RoomBlock,
)

# Images are set by name only: the pages just need a URL, and generating real
# files would leave them behind in static/ and media/ after every test run.


class CityFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = City

    city_id = factory.fuzzy.FuzzyText(length=100)
    name = factory.fuzzy.FuzzyText(length=100)


class HotelFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Hotel

    city = factory.SubFactory(CityFactory)
    hotel_id = factory.fuzzy.FuzzyText(length=100)
    name = factory.fuzzy.FuzzyText(length=100)
    image = factory.Sequence(lambda n: f"example_hotel_{n}.jpg")
    description = factory.fuzzy.FuzzyText(length=100)
    price = factory.fuzzy.FuzzyDecimal(100.00)
    is_available = factory.fuzzy.FuzzyChoice([True, False])

    @factory.post_generation
    def highlight(self, create, extracted, **kwargs):
        if not create:
            return
        if extracted:
            for highlight in extracted:
                self.highlight.add(highlight)


class RoomFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Room

    hotel = factory.SubFactory(HotelFactory)
    image = factory.Sequence(lambda n: f"example_room_{n}.jpg")
    price = factory.fuzzy.FuzzyDecimal(100.00)
    description = factory.fuzzy.FuzzyText(length=100)
    inventory = 5


def _nights(order):
    return (order.end_date - order.start_date).days


class OrderFactory(factory.django.DjangoModelFactory):
    """
    A confirmed order, like the booking service makes it: with ``total_price``
    and one ``OrderNight`` per night at the room price.

    ``OrderFactory(nights=False)`` skips the nights. Note: ``create_booking``
    checks availability, this factory does not.
    """

    class Meta:
        model = Order
        skip_postgeneration_save = True

    hotel = factory.SubFactory(HotelFactory)
    room = factory.SubFactory(RoomFactory, hotel=factory.SelfAttribute("..hotel"))
    start_date = factory.fuzzy.FuzzyDate(datetime.date(2020, 1, 1))
    end_date = factory.LazyAttribute(
        lambda order: order.start_date + datetime.timedelta(days=2)
    )
    first_name = factory.fuzzy.FuzzyText(length=100)
    last_name = factory.fuzzy.FuzzyText(length=100)
    email = factory.Sequence(lambda n: f"gast{n}@example.com")
    address = factory.fuzzy.FuzzyText(length=100)
    zipcode = factory.fuzzy.FuzzyText(length=6)
    country = "NL"
    status = OrderStatus.CONFIRMED
    payment_status = PaymentStatus.AT_HOTEL
    total_price = factory.LazyAttribute(
        lambda order: (Decimal(str(order.room.price)) * _nights(order)).quantize(
            Decimal("0.01")
        )
    )

    @factory.post_generation
    def nights(self, create, extracted, **kwargs):
        if not create or extracted is False:
            return
        price = Decimal(str(self.room.price)).quantize(Decimal("0.01"))
        OrderNight.objects.bulk_create(
            OrderNight(
                order=self,
                date=self.start_date + datetime.timedelta(days=offset),
                price=price,
            )
            for offset in range(_nights(self))
        )


class RoomBlockFactory(factory.django.DjangoModelFactory):
    """One room blocked for two nights, starting a week from today."""

    class Meta:
        model = RoomBlock

    room = factory.SubFactory(RoomFactory)
    start_date = factory.LazyFunction(
        lambda: timezone.localdate() + datetime.timedelta(days=7)
    )
    end_date = factory.LazyAttribute(
        lambda block: block.start_date + datetime.timedelta(days=2)
    )
    quantity = 1
    reason = "Onderhoud"


class HighlightFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Highlight

    icon = factory.Sequence(lambda n: f"highlight_img/example_{n}.png")
    name = factory.fuzzy.FuzzyText(length=100)
