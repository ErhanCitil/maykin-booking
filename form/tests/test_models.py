from django.test import TestCase
from ..models import *
from django_webtest import WebTest
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

class ContactFormWebTest(WebTest):
    def test_contactform(self):
        form = self.app.get('/contact/').form
        form['naamenachternaam'] = 'John Doe'
        form['email'] = 'johndoe@gmail.nl'
        form['onderwerp'] = 'Test'
        form['bericht'] = 'Dit is een test bericht!'
        response = form.submit().follow()
        self.assertEqual(response.status_code, 200)