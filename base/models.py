from django.db import models

# Create your models here.
# Hier maak ik een database model aan met de naam Data. 
class Data(models.Model):
    city_id = models.CharField(max_length=100)
    city_name = models.CharField(max_length=100)
    hotel_id = models.CharField(max_length=100)
    hotel_name = models.CharField(max_length=100)