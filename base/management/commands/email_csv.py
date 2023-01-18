from django.core.mail import EmailMessage, send_mail
from django.core.management.base import BaseCommand
from base.models import City, Hotel

class Command(BaseCommand):

    help = "Create a CSV or PDF file with the data from the database and sent it to the user"

    def handle(self, *args, **options):

        hotel = Hotel.objects.all()

        file = open('data.csv', 'w')

        for h in hotel:

            file.write(f'{h.city.city_name};{h.hotel_name};{h.hotel_id}')

        file.close()

        mail = EmailMessage(

            'CSV file',

            'This is the CSV file with the data from the database',

            'test@gmail.nl',

            ['erhan.citil@maykinmedia.nl'],

        )

        mail.attach_file('data.csv')

        mail.send()

        #  Attachment files kunnen niet verstuurd worden met de methode send_mail(), daarom heb ik gebruik gemaakt van EmailMessage. 

    #  send_mail maakt de variabele een integer, returnt een 0 of een 1 en daarom krijg je een error als je een attachment file probeert te versturen.