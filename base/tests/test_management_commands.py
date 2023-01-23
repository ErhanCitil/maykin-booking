from ..models import Hotel, Room, City
from django.test import TestCase
from .factories import CityFactory, HotelFactory, RoomFactory
from django.core.management import call_command
from django.core import mail 
class DataFrameTest(TestCase):
    def test_dataframe_management_command(self):
        call_command('dataframe')
        self.assertEqual(City.objects.count(), 196)

    def test_fileimport_management_command(self):
        call_command('fileimport')
        self.assertEqual(City.objects.count(), 196)

class EmailCSVTest(TestCase):
    def test_emailcsv_management_command(self):
        call_command('email_csv')
        self.assertEqual(len(mail.outbox), 1)