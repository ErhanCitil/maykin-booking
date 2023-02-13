from rest_framework import serializers
from base.models import City, Hotel, Room, Order, Highlight

class CitySerializer(serializers.ModelSerializer):
    class Meta:
        model = City
        fields = ['url', 'id', 'city_id', 'name']

class HotelSerializer(serializers.ModelSerializer):
    class Meta:
        model = Hotel
        fields = ['url', 'id', 'hotel_id', 'name', 'image', 'description', 'price', 'is_available', 'terms']
        