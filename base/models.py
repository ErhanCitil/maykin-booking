from django.db import models
from django_countries.fields import CountryField
# Create your models here.
class City(models.Model):
    city_id = models.CharField(max_length=100)
    name = models.CharField(max_length=100)

    def __str__(self):
        return self.name

class Hotel(models.Model):
    city = models.ForeignKey(City, related_name='test', on_delete=models.CASCADE)
    hotel_id = models.CharField(max_length=100)
    name = models.CharField(max_length=100)
    image = models.ImageField(upload_to='hotel_img/', null=True, blank=True)
    description = models.TextField(default='', blank=True, null=True)
    price = models.DecimalField(max_digits=6, decimal_places=2, default=0.00)
    is_available = models.BooleanField(default=True)

    def __str__(self):
        return self.name

SINGLE = "Single"
DOUBLE = "Double"
FAMILY = "Family"

ROOM_CHOICES = (
    (SINGLE, "Single"),
    (DOUBLE, "Double"),
    (FAMILY, "Family"),
)

class Room(models.Model):
    hotel = models.ForeignKey(Hotel, related_name='room', on_delete=models.CASCADE)
    image = models.ImageField(upload_to='room_img/')
    price = models.DecimalField(max_digits=6, decimal_places=2, default=0.00)
    description = models.TextField(default='', blank=True, null=True)
    is_available = models.BooleanField(default=True)
    room_type = models.CharField(max_length=50, choices=ROOM_CHOICES)

    def __str__(self):
        return self.room_type

class Order(models.Model):
    room = models.ForeignKey(Room, related_name='room', on_delete=models.CASCADE, null=True, blank=True)
    hotel = models.ForeignKey(Hotel, related_name='order', on_delete=models.CASCADE, null=True, blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField(max_length=100)
    address = models.CharField(max_length=100)
    zipcode = models.CharField(max_length=6)
    country = CountryField(default='NL')

    def __str__(self):
        return str(self.id)

    @property
    def total_price(self):
        return self.room.price * (self.end_date - self.start_date).days 