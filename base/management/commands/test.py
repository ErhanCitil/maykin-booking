from django.core.management.base import BaseCommand
from django.db.models import Count
from base.models import Data
from datetime import timedelta, datetime
from django.utils.timezone import utc
import pandas as pd
import requests
from requests.auth import HTTPBasicAuth
import io

class Command(BaseCommand):

    def handle(self, *args, **options):
      cityurl = "http://rachel.maykinmedia.nl/djangocase/city.csv"
      hotelurl = "http://rachel.maykinmedia.nl/djangocase/hotel.csv"
      responsecity = requests.get(cityurl).content
      responsehotel = requests.get(hotelurl).content
      # Hier open ik de csv file en sla ik deze op in een dataframe ik maak gebruik van de library genaamd Pandas. 
      citydata = pd.read_csv(io.StringIO(responsecity.decode('utf-8')), names=['city_id', 'city_name'], sep=";", header=None)
      hoteldata = pd.read_csv(io.StringIO(responsehotel.decode('utf-8')), names=['city_id', 'hotel_id', 'hotel_name', '3'], sep=";", header=None)

      # Hier drop ik de kolom 3 omdat deze missende values bevat, daarna merge ik de twee dataframes op basis van de city_id.
      hoteldata.drop(columns=['3'], inplace=True)
      df = pd.merge(citydata, hoteldata, on='city_id')
  
      for index, row in df.iterrows():
            data = Data()
            data.city_id = row['city_id']
            data.city_name = row['city_name']
            data.hotel_id = row['hotel_id']
            data.hotel_name = row['hotel_name']
            data.save()
            