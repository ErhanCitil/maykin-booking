from django import forms
from .models import ContactForm
from base.models import Order, ROOM_CHOICES
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

class OrderForm(forms.ModelForm):

    class Meta:
        model = Order
        fields = ('start_date', 'end_date', 'room')
        widgets = {
            'start_date': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'end_date': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'room_type': forms.ChoiceField(choices=ROOM_CHOICES, widget=forms.RadioSelect),
        }

class CustomerForm(OrderForm):
    first_name = forms.CharField(max_length=100, widget=forms.TextInput(attrs={'class': 'form-control'}))
    last_name = forms.CharField(max_length=100, widget=forms.TextInput(attrs={'class': 'form-control'}))
    email = forms.EmailField(max_length=100, widget=forms.TextInput(attrs={'class': 'form-control'}))
    address = forms.CharField(max_length=100, widget=forms.TextInput(attrs={'class': 'form-control'}))
    zipcode = forms.CharField(max_length=6, widget=forms.TextInput(attrs={'class': 'form-control'}))
    country = forms.ChoiceField(choices=countries, widget=forms.Select(attrs={'class': 'form-control'}))
