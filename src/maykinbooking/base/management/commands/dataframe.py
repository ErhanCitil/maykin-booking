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
            hotel = Hotel(city=city, hotel_id=row['hotel_id'], name=row['name'], image='amsterdamibishotel.jpg', description='''Welcome to the "Riverside Oasis Hotel," a luxurious retreat in the heart of Amsterdam. Our hotel is dedicated to providing you with an unforgettable experience, where modern comfort meets timeless elegance. As you step into our elegant lobby, you'll be greeted by our friendly and attentive staff, who are committed to ensuring your stay is nothing short of exceptional. Our concierge is at your service, ready to assist with restaurant reservations, transportation, and any other requests you may have.
Accommodations at the "Riverside Oasis Hotel" are designed to pamper and rejuvenate. Choose from a variety of beautifully appointed rooms and suites, each featuring stylish decor, plush furnishings, and an array of amenities to make you feel at home. From spacious work desks to high-speed Wi-Fi, we've taken care of every detail to cater to both leisure and business travelers.
Indulge your taste buds at our exquisite on-site restaurants, where our talented chefs prepare a diverse range of culinary delights. Whether you crave international cuisine, local flavors, or a fine dining experience, our dining options will delight even the most discerning palates. Don't forget to unwind with a signature cocktail at our elegant bar.
For relaxation and recreation, the "Riverside Oasis Hotel" offers a range of facilities, including a state-of-the-art fitness center, a rejuvenating spa, and a sparkling swimming pool. Stay active, pamper yourself, or simply lounge in comfort—the choice is yours.
If you're planning a special event, look no further than our versatile event spaces. From weddings and conferences to intimate gatherings, our experienced event team will ensure every detail is tailored to your specific needs.
The "Riverside Oasis Hotel" is also conveniently located near key attractions and landmarks in Amsterdam, making it the perfect starting point for exploring the city's rich culture and attractions. Our concierge can assist you in planning your outings, from museum visits to guided tours.
At the "Riverside Oasis Hotel," we take pride in our commitment to sustainability and environmental responsibility. We strive to minimize our carbon footprint and contribute positively to the community.
Whether you're here for business or leisure, the "Riverside Oasis Hotel" promises an experience that combines sophistication with warm hospitality. Book your stay with us and discover the epitome of luxury and comfort in Amsterdam.''')
            hotel.save()
            single_room = Room(hotel=hotel, image='singlehotelroom.webp', price=50.00, description='''Welcome to our cozy single room, designed to provide you with a comfortable and pleasant stay. Situated in the heart of our charming hotel, this room offers the perfect blend of modern amenities and a warm, inviting atmosphere.
Upon entering, you'll be greeted by a spacious and tastefully decorated room, featuring a comfortable single bed adorned with soft, luxurious linens. The room's neutral color palette creates a soothing ambiance, allowing you to relax after a day of activities.
For your convenience, the room is equipped with a flat-screen TV, offering a variety of channels for your entertainment. A well-lit work desk is also provided, in case you need to catch up on some work or plan your next adventure.
The ensuite bathroom is equipped with modern fixtures and amenities, including a refreshing shower, fluffy towels, and complimentary toiletries. Whether you're preparing for a day of exploration or winding down for a peaceful night's sleep, you'll find everything you need in this well-appointed bathroom.
To enhance your comfort, the room is equipped with individually controlled air conditioning, allowing you to set the perfect temperature to suit your preference. You'll also find a mini-fridge stocked with refreshments and a coffee maker for your morning pick-me-up.
Step out onto the private balcony and enjoy the views of the city skyline or our tranquil garden. It's the perfect spot to savor a quiet moment with a cup of coffee or a glass of wine.
Our dedicated housekeeping team ensures that your room is kept spotless throughout your stay, providing fresh linens and a turndown service upon request.
Whether you're traveling for business or leisure, our single room is designed to cater to your needs. We look forward to making your stay with us a comfortable and memorable one.
Book now and experience the comfort and hospitality that our hotel has to offer!''',
                        is_available=True, room_type=ROOM_CHOICES[0][0])
            family_room = Room(hotel=hotel, image='familyroom.webp', price=150.00, description='''Welcome to our spacious and inviting family room, designed to provide your entire family with a comfortable and memorable stay. Nestled in the heart of our charming hotel, this room offers a perfect blend of modern amenities and a warm, family-friendly ambiance. As you enter, you'll be greeted by a generously sized room, tastefully decorated to create a welcoming atmosphere. The room features a combination of a plush queen-sized bed for the parents and a separate area with twin beds for the kids, all adorned with soft, luxurious linens.
For your entertainment, the room is equipped with a flat-screen TV with a variety of channels to keep everyone entertained. A well-lit work desk is also provided, in case you need to catch up on some work or plan your family's day of adventure.
The spacious ensuite bathroom is equipped with modern fixtures and amenities, including a bathtub and a separate shower for your convenience. Fluffy towels and complimentary toiletries are provided, ensuring everyone's comfort.
To add to your family's comfort, the room is equipped with individually controlled air conditioning, allowing you to set the perfect temperature for everyone's preference. You'll also find a mini-fridge stocked with refreshments and a coffee maker for your morning caffeine fix.
Step out onto the private balcony and enjoy the views of the surrounding area or our lovely garden. It's the perfect spot to relax with your family and enjoy a quiet moment together.
Our dedicated housekeeping team ensures that your family room is kept clean and tidy throughout your stay, providing fresh linens and a turndown service as per your request.
Whether you're on a family vacation, a weekend getaway, or just looking for a comfortable space for your loved ones, our family room is designed to cater to your every need. We look forward to making your family's stay with us a memorable one.
Book now and experience the comfort and hospitality that our hotel has to offer for families!''',
                                 is_available=True, room_type=ROOM_CHOICES[2][0])

            double_room = Room(hotel=hotel, image='hotelroom.webp', price=100.00, description='''Welcome to our cozy double room, designed to provide you and your companion with a comfortable and memorable stay. Nestled in the heart of our charming hotel, this room offers a perfect blend of modern amenities and a warm, inviting ambiance. As you enter, you'll be greeted by a spacious and tastefully decorated room, featuring a plush queen-sized bed adorned with soft, luxurious linens. The room's neutral color palette creates a calming atmosphere, allowing you to relax and unwind after a day of exploring the city.
For your convenience, the room is equipped with a flat-screen TV, offering a variety of channels for your entertainment. A well-lit work desk is also provided, in case you need to catch up on some work or plan your next adventure. Complimentary high-speed Wi-Fi ensures you stay connected throughout your stay.
The ensuite bathroom is equipped with modern fixtures and amenities, including a rainfall shower, fluffy towels, and complimentary toiletries. Whether you're preparing for a day of sightseeing or winding down for a peaceful night's sleep, you'll find everything you need in this well-appointed bathroom.
To add to your comfort, the room is equipped with individually controlled air conditioning, allowing you to set the perfect temperature for your preference. You'll also find a mini-fridge stocked with refreshments and a coffee maker for your morning caffeine fix.
Step out onto the private balcony and take in the picturesque views of the city or our lush garden. It's the perfect spot to enjoy a quiet moment with a cup of coffee or a glass of wine.
Our dedicated housekeeping team ensures that your room is kept immaculate throughout your stay, providing fresh linens and a turndown service as per your preference.
Whether you're here for a romantic getaway, a business trip, or simply exploring the city with a friend, our double room for two persons is designed to cater to your every need. We look forward to making your stay with us a memorable one.
Book now and experience the comfort and hospitality that our hotel has to offer!''', is_available=True, room_type=ROOM_CHOICES[1][0])

            single_room.save()
            family_room.save()
            double_room.save()


            hotel.room.add(single_room)
            hotel.room.add(family_room)
            hotel.room.add(double_room)
            hotel.save()
