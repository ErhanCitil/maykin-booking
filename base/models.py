from django.db import models
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

class Room(models.Model):
    hotel = models.ForeignKey(Hotel, related_name='room', on_delete=models.CASCADE)
    image = models.ImageField(upload_to='room_img/')
    title = models.CharField(max_length=100)
    price = models.DecimalField(max_digits=6, decimal_places=2, default=0.00)
    description = models.TextField(default='', blank=True, null=True)

    def __str__(self):
        return self.title