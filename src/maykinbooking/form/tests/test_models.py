from django.test import TestCase
from django.urls import reverse

from django_webtest import WebTest

from ..models import ContactForm
from .factories import ContactFormFactory


class ContactFormTestCase(TestCase):
    def setUp(self):
        self.contact = ContactFormFactory()
        self.contact2 = ContactFormFactory()

    def assertSaved(self, field, value, value2):
        setattr(self.contact, field, value)
        setattr(self.contact2, field, value2)
        self.contact.save()
        self.contact2.save()
        self.contact.refresh_from_db()
        self.contact2.refresh_from_db()
        self.assertEqual(getattr(self.contact, field), value)
        self.assertEqual(getattr(self.contact2, field), value2)

    def test_naamenachternaam(self):
        self.assertSaved("naamenachternaam", "John Doe", "Jane Doe")

    def test_email(self):
        self.assertSaved("email", "info@johndoe.nl", "info@janedoe.nl")

    def test_onderwerp(self):
        self.assertSaved("onderwerp", "Test", "Test")

    def test_bericht(self):
        self.assertSaved(
            "bericht", "Dit is een Test Bericht!", "Dit is een de tweede test bericht!)"
        )

    def test_str_is_the_name(self):
        self.contact.naamenachternaam = "John Doe"
        self.assertEqual(str(self.contact), "John Doe")


class ContactFormWebTest(WebTest):
    def test_contactform(self):
        form = self.app.get(reverse("contact")).form
        form["naamenachternaam"] = "John Doe"
        form["email"] = "johndoe@gmail.nl"
        form["onderwerp"] = "Test"
        form["bericht"] = "Dit is een test bericht!"
        response = form.submit()

        self.assertRedirects(response, reverse("index"))
        self.assertEqual(response.follow().status_code, 200)
        contact = ContactForm.objects.get()
        self.assertEqual(contact.naamenachternaam, "John Doe")
        self.assertEqual(contact.email, "johndoe@gmail.nl")
        self.assertEqual(contact.bericht, "Dit is een test bericht!")
