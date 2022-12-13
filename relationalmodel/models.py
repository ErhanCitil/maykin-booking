from django.db import models

# Create your models here.
class Stad(models.Model):
    city_id = models.CharField(max_length=100)
    city_name = models.CharField(max_length=100)

    def __str__(self):
        return self.city_name

class Hotel(models.Model):
    stad = models.ForeignKey(Stad, on_delete=models.CASCADE)
    hotel_id = models.CharField(max_length=100)
    hotel_name = models.CharField(max_length=100)

    def __str__(self):
        return self.hotel_name

