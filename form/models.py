from django.db import models

# Create your models here.
class ContactForm(models.Model):
    naamenachternaam = models.CharField(max_length=100)
    email = models.CharField(max_length=100)
    onderwerp = models.CharField(max_length=100)
    bericht = models.CharField(max_length=100)

    def __str__(self):
        return self.naamenachternaam