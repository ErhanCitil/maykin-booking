from django.core.management.base import BaseCommand
from maykinbooking.base.models import City, Hotel, Room, ROOM_CHOICES
import pandas as pd
import requests
from requests.auth import HTTPBasicAuth
import io


class Command(BaseCommand):

    def handle(self, *args, **options):
        responsecity = requests.get('http://rachel.maykinmedia.nl/djangocase/city.csv',
                                    auth=HTTPBasicAuth('python-demo', 'claw30_bumps')).content
        responsehotel = requests.get('http://rachel.maykinmedia.nl/djangocase/hotel.csv',
                                     auth=HTTPBasicAuth('python-demo', 'claw30_bumps')).content

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
            hotel = Hotel(city=city, hotel_id=row['hotel_id'], name=row['name'])
            hotel.save()
            room = Room(hotel=hotel, image='static/hotelroom.webp', price=100.00, description='''Welcome to our cozy double room, designed to provide you and your companion with a comfortable and memorable stay. Nestled in the heart of our charming hotel, this room offers a perfect blend of modern amenities and a warm, inviting ambiance. As you enter, you'll be greeted by a spacious and tastefully decorated room, featuring a plush queen-sized bed adorned with soft, luxurious linens. The room's neutral color palette creates a calming atmosphere, allowing you to relax and unwind after a day of exploring the city.
For your convenience, the room is equipped with a flat-screen TV, offering a variety of channels for your entertainment. A well-lit work desk is also provided, in case you need to catch up on some work or plan your next adventure. Complimentary high-speed Wi-Fi ensures you stay connected throughout your stay.
The ensuite bathroom is equipped with modern fixtures and amenities, including a rainfall shower, fluffy towels, and complimentary toiletries. Whether you're preparing for a day of sightseeing or winding down for a peaceful night's sleep, you'll find everything you need in this well-appointed bathroom.
To add to your comfort, the room is equipped with individually controlled air conditioning, allowing you to set the perfect temperature for your preference. You'll also find a mini-fridge stocked with refreshments and a coffee maker for your morning caffeine fix.
Step out onto the private balcony and take in the picturesque views of the city or our lush garden. It's the perfect spot to enjoy a quiet moment with a cup of coffee or a glass of wine.
Our dedicated housekeeping team ensures that your room is kept immaculate throughout your stay, providing fresh linens and a turndown service as per your preference.
Whether you're here for a romantic getaway, a business trip, or simply exploring the city with a friend, our double room for two persons is designed to cater to your every need. We look forward to making your stay with us a memorable one.
Book now and experience the comfort and hospitality that our hotel has to offer!''', is_available=True, room_type=ROOM_CHOICES[1][0])
            room.save()
            hotel.room.add(room)
            hotel.save()
