from django.core.management.base import BaseCommand
from base.models import City, Hotel
import pandas as pd
import requests
from requests.auth import HTTPBasicAuth
import io

class Command(BaseCommand):

    def handle(self, *args, **options):
      responsecity = requests.get('http://rachel.maykinmedia.nl/djangocase/city.csv',
            auth = HTTPBasicAuth('python-demo', 'claw30_bumps')).content
      responsehotel = requests.get('http://rachel.maykinmedia.nl/djangocase/hotel.csv',
            auth = HTTPBasicAuth('python-demo', 'claw30_bumps')).content
            
      # Hier open ik de csv file en sla ik deze op in een dataframe ik maak gebruik van de library genaamd Pandas.
      citydata = pd.read_csv(io.StringIO(responsecity.decode('utf-8')), names=['city_id', 'name'], sep=";", header=None)
      hoteldata = pd.read_csv(io.StringIO(responsehotel.decode('utf-8')), names=['city_id', 'hotel_id', 'name', '3'], sep=";", header=None)

      # Hier drop ik de kolom 3 omdat deze missende values bevat, daarna merge ik de twee dataframes op basis van de city_id.
      hoteldata.drop(columns=['3'], inplace=True)
      
      for index, row in citydata.iterrows():
            city = City(city_id=row['city_id'], name=row['name'])
            city.save()

      for index, row in hoteldata.iterrows():
            city = City.objects.get(city_id=row['city_id'])
            hotel = Hotel(city = city, hotel_id=row['hotel_id'], name=row['name'], image='https://picsum.photos/64/100')
            hotel.save()