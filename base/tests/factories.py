import factory, factory.fuzzy
from base.models import Hotel, Room, City, Highlight

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
    image = factory.django.ImageField()
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
    image = factory.django.ImageField()
    price = factory.fuzzy.FuzzyDecimal(100.00)
    description = factory.fuzzy.FuzzyText(length=100)

class HighlightFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Highlight

    icon = factory.django.ImageField()
    name = factory.fuzzy.FuzzyText(length=100)
