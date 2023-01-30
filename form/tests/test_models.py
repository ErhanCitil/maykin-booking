from django.test import TestCase
from ..models import ContactForm
from django_webtest import WebTest
from .factories import ContactFormFactory
# Create your tests here.
class ContactFormTestCase(TestCase):
    def setUp(self):
        self.contact = ContactFormFactory()
        self.contact2 = ContactFormFactory()

    def test_naamenachternaam(self):
        self.contact.naamenachternaam = 'John Doe'
        self.contact2.naamenachternaam = 'Jane Doe'
        self.assertEqual(self.contact.naamenachternaam, 'John Doe')
        self.assertEqual(self.contact2.naamenachternaam, 'Jane Doe')

    def test_email(self):
        self.contact.email = 'info@johndoe.nl'
        self.contact2.email = 'info@janedoe.nl'
        self.assertEqual(self.contact.email, 'info@johndoe.nl')
        self.assertEqual(self.contact2.email, 'info@janedoe.nl')

    def test_onderwerp(self):
        self.contact.onderwerp = 'Test'
        self.contact2.onderwerp = 'Test'
        self.assertEqual(self.contact.onderwerp, 'Test')
        self.assertEqual(self.contact2.onderwerp, 'Test')

    def test_bericht(self):
        self.contact.bericht = 'Dit is een Test Bericht!'
        self.contact2.bericht = 'Dit is een de tweede test bericht!)'
        self.assertEqual(self.contact.bericht, 'Dit is een Test Bericht!')
        self.assertEqual(self.contact2.bericht, 'Dit is een de tweede test bericht!)')

class ContactFormWebTest(WebTest):
    def test_contactform(self):
        form = self.app.get('/contact/').form
        form['naamenachternaam'] = 'John Doe'
        form['email'] = 'johndoe@gmail.nl'
        form['onderwerp'] = 'Test'
        form['bericht'] = 'Dit is een test bericht!'
        response = form.submit().follow()
        self.assertEqual(response.status_code, 200)