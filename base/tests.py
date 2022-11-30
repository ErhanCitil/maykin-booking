from django.test import TestCase
from .models import Data
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

