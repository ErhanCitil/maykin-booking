from django.test import TestCase
from .models import *
# Create your tests here.

class DataTestCase(TestCase):
    def setUp(self):
        Data.objects.create(city_id='1', city_name='Amsterdam', hotel_id='1', hotel_name='Hotel1')
        Data.objects.create(city_id='2', city_name='Rotterdam', hotel_id='2', hotel_name='Hotel2')
        Data.objects.create(city_id='3', city_name='Utrecht', hotel_id='3', hotel_name='Hotel3')
        Data.objects.create(city_id='4', city_name='Den Haag', hotel_id='4', hotel_name='Hotel4')
        Data.objects.create(city_id='5', city_name='Eindhoven', hotel_id='5', hotel_name='Hotel5')
    
    def test_city_id(self):
        data = Data.objects.get(city_id='1')
        self.assertEqual(data.city_id, '1')

    def test_city_name(self):
        data = Data.objects.get(city_name='Amsterdam')
        self.assertEqual(data.city_name, 'Amsterdam')

class ContactFormTestCase(TestCase):
    def setUp(self):
        ContactForm.objects.create(naamenachternaam='John Doe', email = 'info@johndoe.nl', onderwerp = 'Test', bericht = 'Dit is een Test Bericht!')
        ContactForm.objects.create(naamenachternaam='Jane Doe', email = 'info@janedoe.nl', onderwerp = 'Test', bericht = 'Dit is een de tweede test bericht!)')

    def test_naamenachternaam(self):
        john = ContactForm.objects.get(naamenachternaam='John Doe')
        jane = ContactForm.objects.get(naamenachternaam='Jane Doe')
        self.assertEqual(john.naamenachternaam, 'John Doe')
        self.assertEqual(jane.naamenachternaam, 'Jane Doe')
