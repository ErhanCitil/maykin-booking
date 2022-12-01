from django.db import models

# Create your models here.
# Hier maak ik een database model aan met de naam Data. 
class Data(models.Model):
    city_id = models.CharField(max_length=100)
    city_name = models.CharField(max_length=100)
    hotel_id = models.CharField(max_length=100)
    hotel_name = models.CharField(max_length=100)

    def __str__(self):
        return self.city_name

    
class ContactForm(models.Model):
    naamenachternaam = models.CharField(max_length=100)
    email = models.CharField(max_length=100)
    onderwerp = models.CharField(max_length=100)
    bericht = models.CharField(max_length=100)

    def __str__(self):
        return self.naamenachternaam
    