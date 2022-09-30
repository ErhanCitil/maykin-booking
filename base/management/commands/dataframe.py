from django.core.management.base import BaseCommand
from django.db.models import Count
from base.models import Data
from datetime import timedelta, datetime
from django.utils.timezone import utc
import pandas as pd

class Command(BaseCommand):

    def handle(self, *args, **options):

      citydata = pd.read_csv('Case Maykin Media - Django - CSV data/city.csv', names=['city_id', 'city_name'], sep=";", header=None)
      hoteldata = pd.read_csv('Case Maykin Media - Django - CSV data/hotel.csv', names=['city_id', 'hotel_id', 'hotel_name', '3'], sep=";", header=None)

      hoteldata.drop(columns=['3'], inplace=True)
      df = pd.merge(citydata, hoteldata, on='city_id')

      for index, row in df.iterrows():
            Data.objects.create(
                city_id=row['city_id'],
                city_name=row['city_name'],
                hotel_id=row['hotel_id'],
                hotel_name=row['hotel_name'],
            )