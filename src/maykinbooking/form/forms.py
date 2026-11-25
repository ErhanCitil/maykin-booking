from datetime import date

from django import forms

from maykinbooking.base.models import Order, Room

from .models import ContactForm


class FormContact(forms.ModelForm):
    class Meta:
        model = ContactForm
        fields = ("naamenachternaam", "email", "onderwerp", "bericht")
        widgets = {
            "naamenachternaam": forms.TextInput(attrs={"class": "form-control"}),
            "email": forms.TextInput(attrs={"class": "form-control"}),
            "onderwerp": forms.TextInput(attrs={"class": "form-control"}),
            "bericht": forms.Textarea(attrs={"class": "form-control"}),
        }
        labels = {
            "naamenachternaam": "Naam en achternaam",
            "email": "E-mailadres",
            "onderwerp": "Onderwerp",
            "bericht": "Bericht",
        }


MAX_NIGHTS = 30


class RoomChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, room):
        return f"{room.get_room_type_display()} · € {room.price} per nacht"


class StayForm(forms.Form):
    """Checkout step 1: dates and one of the hotel's available rooms."""

    start_date = forms.DateField(
        label="Inchecken",
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )
    end_date = forms.DateField(
        label="Uitchecken",
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )
    room = RoomChoiceField(
        label="Kamer",
        queryset=Room.objects.none(),
        empty_label=None,
        widget=forms.RadioSelect(attrs={"class": "h-5 w-5 shrink-0 accent-[#13243A]"}),
        error_messages={"required": "Kies een kamer."},
    )

    def __init__(self, *args, hotel, **kwargs):
        super().__init__(*args, **kwargs)
        rooms = hotel.room.filter(is_available=True).order_by("price")
        self.fields["room"].queryset = rooms
        if rooms:
            self.fields["room"].initial = rooms[0].pk
        today = date.today().isoformat()
        self.fields["start_date"].widget.attrs["min"] = today
        self.fields["end_date"].widget.attrs["min"] = today

    def clean_start_date(self):
        start_date = self.cleaned_data["start_date"]
        if start_date < date.today():
            raise forms.ValidationError("Je kunt niet inchecken in het verleden.")
        return start_date

    def clean(self):
        cleaned_data = super().clean()
        start_date = cleaned_data.get("start_date")
        end_date = cleaned_data.get("end_date")
        if start_date and end_date:
            nights = (end_date - start_date).days
            if nights < 1:
                self.add_error("end_date", "Uitchecken moet na de incheckdatum zijn.")
            elif nights > MAX_NIGHTS:
                self.add_error(
                    "end_date", f"Je kunt maximaal {MAX_NIGHTS} nachten boeken."
                )
        return cleaned_data


class GuestForm(forms.ModelForm):
    """Checkout step 2: the guest's details."""

    class Meta:
        model = Order
        fields = ("first_name", "last_name", "email", "address", "zipcode", "country")
        labels = {
            "first_name": "Voornaam",
            "last_name": "Achternaam",
            "email": "E-mailadres",
            "address": "Adres",
            "zipcode": "Postcode",
            "country": "Land",
        }
        widgets = {
            "first_name": forms.TextInput(
                attrs={"class": "form-control", "autocomplete": "given-name"}
            ),
            "last_name": forms.TextInput(
                attrs={"class": "form-control", "autocomplete": "family-name"}
            ),
            "email": forms.EmailInput(
                attrs={"class": "form-control", "autocomplete": "email"}
            ),
            "address": forms.TextInput(
                attrs={"class": "form-control", "autocomplete": "street-address"}
            ),
            "zipcode": forms.TextInput(
                attrs={"class": "form-control", "autocomplete": "postal-code"}
            ),
            "country": forms.Select(
                attrs={"class": "form-control", "autocomplete": "country"}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.required = True
        # Allow "1015 CJ" as input: spaces are removed before validation.
        self.fields["zipcode"].max_length = None
        self.fields["zipcode"].validators = []
        self.fields["zipcode"].widget.attrs.pop("maxlength", None)

    def clean_zipcode(self):
        zipcode = self.cleaned_data["zipcode"].replace(" ", "").upper()
        if len(zipcode) > 6:
            raise forms.ValidationError("Vul een postcode van maximaal 6 tekens in.")
        return zipcode


class ConfirmForm(forms.Form):
    """Checkout step 3: review the booking and accept the terms."""

    terms = forms.BooleanField(
        label="Ik ga akkoord met de voorwaarden van het hotel",
        widget=forms.CheckboxInput(
            attrs={"class": "h-5 w-5 mt-0.5 shrink-0 accent-[#13243A]"}
        ),
        error_messages={
            "required": "Je moet akkoord gaan met de voorwaarden om te boeken."
        },
    )
