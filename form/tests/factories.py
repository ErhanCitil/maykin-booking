import factory, factory.fuzzy
from form.models import ContactForm

class ContactFormFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ContactForm

    naamenachternaam = factory.fuzzy.FuzzyText(length=100)
    email = factory.fuzzy.FuzzyText(length=100)
    onderwerp = factory.fuzzy.FuzzyText(length=100)
    bericht = factory.fuzzy.FuzzyText(length=100)

    