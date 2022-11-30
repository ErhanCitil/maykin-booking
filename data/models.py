from django.db import models
import pandas as pd
from pathlib import Path
import os

# Create your models here.

# BASE_DIR = Path(__file__).resolve().parent.parent

# citydata = pd.read_csv(BASE_DIR / 'Case Maykin Media - Django - CSV data/city.csv', names=['city_id', 'city_name'], sep=";", header=None).to_sql('data_city', if_exists='replace', index=False)
# hoteldata = pd.read_csv(BASE_DIR / 'Case Maykin Media - Django - CSV data/hotel.csv', names=['city_id', 'hotel_id', 'hotel_name', '3'], sep=";", header=None).to_sql('data_hotel', if_exists='replace', index=False)

# hoteldata.drop(columns=['3'], inplace=True)

# df = pd.merge(citydata, hoteldata, on='city_id')

class Data(models.Model):
    city_id = models.CharField(max_length=100)
    city_name = models.CharField(max_length=100)
    hotel_id = models.CharField(max_length=100)
    hotel_name = models.CharField(max_length=100)

