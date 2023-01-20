from django.test import TestCase
from ..models import *
# Create your tests here.
class ContactFormTestCase(TestCase):
    def setUp(self):
        ContactForm.objects.create(naamenachternaam='John Doe', email = 'info@johndoe.nl', onderwerp = 'Test', bericht = 'Dit is een Test Bericht!')
        ContactForm.objects.create(naamenachternaam='Jane Doe', email = 'info@janedoe.nl', onderwerp = 'Test', bericht = 'Dit is een de tweede test bericht!)')

    def test_naamenachternaam(self):
        john = ContactForm.objects.get(naamenachternaam='John Doe')
        jane = ContactForm.objects.get(naamenachternaam='Jane Doe')
        self.assertEqual(john.naamenachternaam, 'John Doe')
        self.assertEqual(jane.naamenachternaam, 'Jane Doe')

    def test_email(self):
        john = ContactForm.objects.get(email='info@johndoe.nl')
        jane = ContactForm.objects.get(email='info@janedoe.nl')
        self.assertEqual(john.email, 'info@johndoe.nl')
        self.assertEqual(jane.email, 'info@janedoe.nl')

    def test_onderwerp(self):
      for onderwerp in ContactForm.objects.all():
        self.assertEqual(onderwerp.onderwerp, 'Test')

    def test_bericht(self):
        john = ContactForm.objects.get(bericht='Dit is een Test Bericht!')
        jane = ContactForm.objects.get(bericht='Dit is een de tweede test bericht!)')
        self.assertEqual(john.bericht, 'Dit is een Test Bericht!')
        self.assertEqual(jane.bericht, 'Dit is een de tweede test bericht!)')