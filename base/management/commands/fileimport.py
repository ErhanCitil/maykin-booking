from django.core.management.base import BaseCommand
from base.models import City, Hotel
import pandas as pd

class Command(BaseCommand):

    def handle(self, *args, **options):

      # Hier open ik de csv file en sla ik deze op in een dataframe ik maak gebruik van de library genaamd Pandas. 
      citydata = pd.read_csv('Case Maykin Media - Django - CSV data/city.csv', names=['city_id', 'name'], sep=";", header=None)
      hoteldata = pd.read_csv('Case Maykin Media - Django - CSV data/hotel.csv', names=['city_id', 'hotel_id', 'name', '3'], sep=";", header=None)

      # Hier drop ik de kolom 3 omdat deze missende values bevat, daarna merge ik de twee dataframes op basis van de city_id.
      hoteldata.drop(columns=['3'], inplace=True)

      for index, row in hoteldata.iterrows():
            city = City(city_id=row['city_id'], name=row['name'])
            city.save()
            hotel = Hotel(city = city, hotel_id=row['hotel_id'], name=row['name'])
            hotel.save()