from rest_framework import generics, viewsets

from maykinbooking.base.models import City, Hotel, Review

from .serializers import CitySerializer, HotelSerializer, ReviewSerializer

# The API is read-only. Cities, hotels and rooms are managed in the staff
# dashboard, which also checks that hotels with bookings are not deleted.


class CityViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = City.objects.all()
    serializer_class = CitySerializer


class HotelViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Hotel.objects.all()
    serializer_class = HotelSerializer


class ReviewViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Review.objects.all()
    serializer_class = ReviewSerializer


class HotelList(generics.ListAPIView):
    queryset = Hotel.objects.all()
    serializer_class = HotelSerializer
