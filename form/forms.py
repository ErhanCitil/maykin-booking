from django import forms
from .models import ContactForm
from base.models import Order
from django_countries import countries

class FormContact(forms.ModelForm):
    class Meta:
        model = ContactForm
        fields = ('naamenachternaam', 'email', 'onderwerp', 'bericht')
        widgets = {
            'naamenachternaam': forms.TextInput(attrs={'class': 'form-control'}),
            'email': forms.TextInput(attrs={'class': 'form-control'}),
            'onderwerp': forms.TextInput(attrs={'class': 'form-control'}),
            'bericht': forms.Textarea(attrs={'class': 'form-control'}),
        }