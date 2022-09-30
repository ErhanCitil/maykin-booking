from django.core.management.base import BaseCommand
from django.db.models import Count
from base.models import Data
from datetime import timedelta, datetime
from django.utils.timezone import utc
import pandas as pd

class Command(BaseCommand):

    def handle(self, *args, **options):

      # Hier open ik de csv file en sla ik deze op in een dataframe ik maak gebruik van de library genaamd Pandas. 
      citydata = pd.read_csv('Case Maykin Media - Django - CSV data/city.csv', names=['city_id', 'city_name'], sep=";", header=None)
      hoteldata = pd.read_csv('Case Maykin Media - Django - CSV data/hotel.csv', names=['city_id', 'hotel_id', 'hotel_name', '3'], sep=";", header=None)

      # Hier drop ik de kolom 3 omdat deze missende values bevat, daarna merge ik de twee dataframes op basis van de city_id.
      hoteldata.drop(columns=['3'], inplace=True)
      df = pd.merge(citydata, hoteldata, on='city_id')

      # Hier mee probeer ik de data op te slaan in mijn sqllite database door middel van een for loop.
      for index, row in df.iterrows():
            Data.objects.create(
                city_id=row['city_id'],
                city_name=row['city_name'],
                hotel_id=row['hotel_id'],
                hotel_name=row['hotel_name'],
            )