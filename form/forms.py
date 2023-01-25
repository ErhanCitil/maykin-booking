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

class OrderForm1(forms.Form):
    room_type = forms.ChoiceField(choices=ROOM_CHOICES, widget=forms.RadioSelect)
    start_date = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}))
    end_date = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}))

class OrderForm2(forms.Form):
    first_name = forms.CharField(max_length=100, widget=forms.TextInput(attrs={'class': 'form-control'}))
    last_name = forms.CharField(max_length=100, widget=forms.TextInput(attrs={'class': 'form-control'}))
    email = forms.EmailField(max_length=100, widget=forms.TextInput(attrs={'class': 'form-control'}))
    address = forms.CharField(max_length=100, widget=forms.TextInput(attrs={'class': 'form-control'}))
    zipcode = forms.CharField(max_length=6, widget=forms.TextInput(attrs={'class': 'form-control'}))
    country = forms.ChoiceField(choices=countries, widget=forms.Select(attrs={'class': 'form-control'}))