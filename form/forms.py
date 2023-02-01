from django import forms
from .models import ContactForm
from base.models import Order, ROOM_CHOICES, Room
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

class OrderForm1(forms.ModelForm):
    class Meta:
        model = Order
        exclude = ('first_name', 'last_name', 'email', 'address', 'zipcode', 'country', 'hotel', 'room')

    start_date = forms.DateField(widget=forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}))
    end_date = forms.DateField(widget=forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}))
    room_type = forms.ChoiceField(widget=forms.Select(attrs={'class': 'form-control'}), choices=ROOM_CHOICES)

class OrderForm2(forms.ModelForm):
    class Meta:
        model = Order
        fields = ('first_name', 'last_name', 'email', 'address', 'zipcode', 'country')
        widgets = {
            'first_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Alex'}),
            'last_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'de Landgraaf'}),
            'email': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'alex@maykin.nl'}),
            'address': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Keizersgracht 117'}),
            'zipcode': forms.TextInput(attrs={'class': 'form-control','placeholder': '1015CJ'}),
            'country': forms.Select(attrs={'class': 'form-control'}, choices=countries),
        }
    terms = forms.BooleanField(error_messages={'required': 'Please accept the terms and conditions.'}, label='I accept the terms and conditions')