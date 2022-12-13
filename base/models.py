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

    def __str__(self):
        return self.hotel_name