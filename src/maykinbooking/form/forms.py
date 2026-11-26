from datetime import timedelta

from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from maykinbooking.base.models import Order, Room
from maykinbooking.base.validators import no_control_characters
from maykinbooking.bookings.services.availability import (
    MAX_DAYS_AHEAD,
    MAX_NIGHTS,
    available_rooms,
    validate_stay_period,
)

from .models import ContactForm

# ``<input type="date">`` only accepts ISO dates; the Dutch input format
# (``%d-%m-%Y``) would leave the field empty.
ISO_DATE = "%Y-%m-%d"


def date_input(**attrs):
    return forms.DateInput(
        format=ISO_DATE, attrs={"class": "form-control", "type": "date", **attrs}
    )


def set_date_limits(start_field, end_field):
    """``min``/``max`` of the date pickers: from today up to the booking horizon."""
    today = timezone.localdate()
    last_arrival = today + timedelta(days=MAX_DAYS_AHEAD)
    start_field.widget.attrs.update(min=today.isoformat(), max=last_arrival.isoformat())
    end_field.widget.attrs.update(
        min=today.isoformat(),
        max=(last_arrival + timedelta(days=MAX_NIGHTS)).isoformat(),
    )


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


# The ``validate_stay_period`` errors that are about the check-in date.
START_DATE_ERRORS = {"past", "too_far"}


def _add_period_error(form, error, *, start_field, end_field):
    """Show a ``validate_stay_period`` error next to the field it is about."""
    field = start_field if error.code in START_DATE_ERRORS else end_field
    form.add_error(field, error)


class AvailabilityFilterForm(forms.Form):
    """
    Optional period on the city page (``?van=&tot=``): only hotels with a free
    room for the whole stay are listed. Both dates empty means: no filter.
    """

    van = forms.DateField(
        label=_("Inchecken"),
        required=False,
        widget=date_input(),
        error_messages={"invalid": _("Vul een geldige incheckdatum in.")},
    )
    tot = forms.DateField(
        label=_("Uitchecken"),
        required=False,
        widget=date_input(),
        error_messages={"invalid": _("Vul een geldige uitcheckdatum in.")},
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        set_date_limits(self.fields["van"], self.fields["tot"])

    def clean(self):
        cleaned_data = super().clean()
        start, end = cleaned_data.get("van"), cleaned_data.get("tot")
        if self.errors or (start is None and end is None):
            return cleaned_data
        if start is None or end is None:
            raise ValidationError(
                _("Vul zowel een incheck- als een uitcheckdatum in."),
                code="incomplete",
            )
        try:
            validate_stay_period(start, end)
        except ValidationError as error:
            _add_period_error(self, error, start_field="van", end_field="tot")
        return cleaned_data

    @property
    def period(self):
        """``(start, end)`` of a valid period, otherwise ``None``."""
        if not self.is_bound or not self.is_valid():
            return None
        start, end = self.cleaned_data.get("van"), self.cleaned_data.get("tot")
        if start is None or end is None:
            return None
        return start, end


class RoomChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, room):
        return f"{room.get_room_type_display()} · € {room.price} per nacht"


class StayForm(forms.Form):
    """
    Checkout step 1: dates and one of the hotel's rooms that is free for them.

    Once the period is known (posted data, or the initial dates from
    ``?van=&tot=``), only the rooms that are free for the whole stay
    (``available_rooms``) can be chosen. A room that is no longer free fails
    validation, so the revalidation of the wizard sends the guest back to this
    step when the room fills up before the last step.
    """

    start_date = forms.DateField(label=_("Inchecken"), widget=date_input())
    end_date = forms.DateField(label=_("Uitchecken"), widget=date_input())
    room = RoomChoiceField(
        label=_("Kamer"),
        queryset=Room.objects.none(),
        empty_label=None,
        widget=forms.RadioSelect(attrs={"class": "h-5 w-5 shrink-0 accent-[#13243A]"}),
        error_messages={
            "required": _("Kies een kamer."),
            "invalid_choice": _(
                "Deze kamer is niet vrij in deze periode. Kies een andere kamer."
            ),
        },
    )

    def __init__(self, *args, hotel, **kwargs):
        super().__init__(*args, **kwargs)
        self.hotel = hotel
        self.period = self._get_period()

        rooms = hotel.room.filter(is_available=True, inventory__gt=0).order_by("price")
        if self.period:
            rooms = available_rooms(rooms, *self.period)
        self.fields["room"].queryset = rooms
        first_room = rooms.first()
        if first_room is not None:
            self.fields["room"].initial = first_room.pk
        # Only meaningful for a known period: the hotel has no free room at all.
        self.no_rooms_available = bool(self.period) and first_room is None

        set_date_limits(self.fields["start_date"], self.fields["end_date"])

    def _field_value(self, name):
        """The posted (or else initial) date of a field, or ``None``."""
        field = self.fields[name]
        if self.is_bound:
            value = field.widget.value_from_datadict(
                self.data, self.files, self.add_prefix(name)
            )
        else:
            value = self.get_initial_for_field(field, name)
        try:
            return field.clean(value)
        except ValidationError:
            return None

    def _get_period(self):
        start, end = self._field_value("start_date"), self._field_value("end_date")
        if start is None or end is None:
            return None
        try:
            validate_stay_period(start, end)
        except ValidationError:
            return None
        return start, end

    def clean(self):
        cleaned_data = super().clean()
        start, end = cleaned_data.get("start_date"), cleaned_data.get("end_date")
        if start is None or end is None:
            return cleaned_data
        try:
            validate_stay_period(start, end)
        except ValidationError as error:
            _add_period_error(
                self, error, start_field="start_date", end_field="end_date"
            )
            return cleaned_data

        if self.no_rooms_available:
            # The room field would add "choose a room"; one message is enough.
            self.errors.pop("room", None)
            raise ValidationError(
                _("Geen kamers vrij in deze periode."), code="no_rooms_available"
            )
        # A room that is not free for this period is not in the queryset of the
        # room field, so it is rejected there ("invalid_choice").
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
        # Allow "1015 CJ" as input: the length is checked in clean_zipcode, after
        # the spaces are removed. Special characters are refused before that.
        # (The other fields get no_control_characters from the model field.)
        zipcode = self.fields["zipcode"]
        zipcode.max_length = None
        zipcode.validators = [no_control_characters]
        zipcode.widget.attrs.pop("maxlength", None)

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
