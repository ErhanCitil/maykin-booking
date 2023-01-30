from django.core.mail import EmailMessage, send_mail
from django.core.management.base import BaseCommand
from base.models import City, Hotel

import csv
from django.http import HttpResponse

class Command(BaseCommand):

    help = "Create a CSV or PDF file with the data from the database and sent it to the user"

    def handle(self, *args, **options):

        hotel = Hotel.objects.all()

        file = open('data.csv', 'w')

        response = HttpResponse(content_type='text/csv', headers={'Content-Disposition': 'attachment; filename="data.csv"'})
        writer = csv.writer(response)
        
        for h in hotel:
            writer.writerow([h.city.name, h.city.city_id, h.name, h.hotel_id])
        
        file.close()

        email = EmailMessage(
            'CSV file',
            'CSV file',
            'test@gmail.nl',
            ['erhan.citil@maykinmedia.nl'],
        )

        email.attach_file('data.csv')
        email.send()
    
    #  Attachment files kunnen niet verstuurd worden met de methode send_mail(), daarom heb ik gebruik gemaakt van EmailMessage. 
    #  send_mail maakt de variabele een integer, returnt een 0 of een 1 en daarom krijg je een error als je een attachment file probeert te versturen.