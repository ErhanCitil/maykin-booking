from django.db import models
# Create your models here.
# Hier maak ik een database model aan met de naam Data. 
class City(models.Model):
    city_id = models.CharField(max_length=100)
    city_name = models.CharField(max_length=100)

    def __str__(self):
        return self.city_name

class Hotel(models.Model):
    city = models.ForeignKey(City, related_name='test', on_delete=models.CASCADE)
    hotel_id = models.CharField(max_length=100)
    hotel_name = models.CharField(max_length=100)
    hotel_image = models.ImageField(upload_to='hotel_img/', default='default.jpg')
    description = models.TextField(default='')
    price = models.DecimalField(max_digits=6, decimal_places=2, default=0.00)
    is_available = models.BooleanField(default=True)

    def __str__(self):
        return self.hotel_name

class Room(models.Model):
    hotel = models.ForeignKey(Hotel, related_name='room', on_delete=models.CASCADE)
    room_image = models.ImageField(upload_to='room_img/')
    room_title = models.CharField(max_length=100)

    def __str__(self):
        return self.room_title