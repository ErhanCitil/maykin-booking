import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from maykinbooking.base.models import Order, Room
from maykinbooking.base.validators import no_control_characters
from maykinbooking.billing.services.pricing import (
    InvalidDiscountCode,
    check_discount_code,
)
from maykinbooking.bookings.services.availability import (
    MAX_DAYS_AHEAD,
    MAX_NIGHTS,
    available_rooms,
    validate_stay_period,
)
from maykinbooking.bookings.services.booking import PaymentMethod

from .models import ContactForm

logger = logging.getLogger(__name__)

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


# One message for every code that cannot be used (unknown, expired, used up,
# ...), so guests cannot find out which codes exist.
INVALID_DISCOUNT_CODE = _("Deze kortingscode is niet geldig.")
DISCOUNT_CODE_THROTTLED = _(
    "Je hebt te vaak een kortingscode geprobeerd. Probeer het over een uur "
    "opnieuw, of ga verder zonder kortingscode."
)


class StayForm(forms.Form):
    """
    Checkout step 1: dates, one of the hotel's rooms that is free for them, and
    optionally a discount code.

    Once the period is known (posted data, or the initial dates from
    ``?van=&tot=``), only the rooms that are free for the whole stay
    (``available_rooms``) can be chosen. A room that is no longer free fails
    validation, so the revalidation of the wizard sends the guest back to this
    step when the room fills up before the last step. The same goes for a
    discount code that can no longer be used.

    ``count_discount_code``: called just before a posted code is looked up;
    returns ``True`` when this client tried too many codes, and the code is
    then refused without looking it up. The checkout only passes it for the
    data the guest posts (not when the stored steps are validated again), so
    every code that is really looked up counts, and nothing else does.
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

    discount_code = forms.CharField(
        label=_("Kortingscode"),
        required=False,
        max_length=40,
        error_messages={"max_length": INVALID_DISCOUNT_CODE},
        widget=forms.TextInput(
            attrs={
                "class": "form-control uppercase",
                "autocomplete": "off",
                "autocapitalize": "characters",
                "spellcheck": "false",
            }
        ),
    )

    def __init__(self, *args, hotel, count_discount_code=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.hotel = hotel
        self.count_discount_code = count_discount_code
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

    def clean_discount_code(self):
        """
        The code as stored (``" zomer-26 "`` -> ``"ZOMER-26"``), or ``""``. Only
        whether it can be used today; its amount follows from the quote.
        """
        code = self.cleaned_data["discount_code"]
        if not code:
            return ""
        if self.count_discount_code is not None and self.count_discount_code():
            raise ValidationError(DISCOUNT_CODE_THROTTLED, code="throttled")
        try:
            discount = check_discount_code(code)
        except InvalidDiscountCode as error:
            # The reason only: the code itself stays out of the log.
            logger.info("discount code refused reason=%s", error.reason)
            raise ValidationError(INVALID_DISCOUNT_CODE, code="invalid_discount_code")
        return discount.code


class GuestForm(forms.ModelForm):
    """Checkout step 2: the guest's details."""

    class Meta:
        model = Order
        fields = (
            "first_name",
            "last_name",
            "email",
            "address",
            "zipcode",
            "city",
            "country",
        )
        labels = {
            "first_name": "Voornaam",
            "last_name": "Achternaam",
            "email": "E-mailadres",
            "address": "Adres",
            "zipcode": "Postcode",
            "city": _("Woonplaats"),
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
            "city": forms.TextInput(
                attrs={"class": "form-control", "autocomplete": "address-level2"}
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
    """
    Checkout step 3: review the booking, choose how to pay and accept the terms.

    ``payment_method``: paying at the hotel (also when left empty), or online
    when ``online_payment`` is offered (the view decides: a provider is set up
    and there is something to pay).

    ``expected_total``: the total on the page the guest confirmed (a hidden
    field). It is never used as the price: ``create_booking`` computes the
    price itself and only compares it, so a changed price is shown before
    anything is booked. Without it nothing is compared.
    """

    payment_method = forms.ChoiceField(
        label=_("Hoe wil je betalen?"),
        required=False,
        widget=forms.RadioSelect(
            attrs={"class": "h-5 w-5 mt-0.5 shrink-0 accent-[#13243A]"}
        ),
        error_messages={
            "invalid_choice": _(
                "Online betalen is voor deze boeking niet mogelijk. Kies betalen "
                "in het hotel."
            )
        },
    )
    expected_total = forms.CharField(required=False, widget=forms.HiddenInput)
    terms = forms.BooleanField(
        label="Ik ga akkoord met de voorwaarden van het hotel",
        widget=forms.CheckboxInput(
            attrs={"class": "h-5 w-5 mt-0.5 shrink-0 accent-[#13243A]"}
        ),
        error_messages={
            "required": "Je moet akkoord gaan met de voorwaarden om te boeken."
        },
    )

    def __init__(self, *args, online_payment=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.online_payment = online_payment
        choices = [(PaymentMethod.AT_HOTEL.value, _("In het hotel betalen"))]
        if online_payment:
            choices.append((PaymentMethod.ONLINE.value, _("Nu online betalen (iDEAL)")))
        field = self.fields["payment_method"]
        field.choices = choices
        field.initial = PaymentMethod.AT_HOTEL.value

    def clean_payment_method(self):
        return self.cleaned_data["payment_method"] or PaymentMethod.AT_HOTEL.value

    def clean_expected_total(self):
        """A ``Decimal``, or ``None`` when the page sent no total."""
        value = self.cleaned_data["expected_total"]
        if not value:
            return None
        try:
            total = Decimal(value) if len(value) <= 20 else None
        except InvalidOperation:
            total = None
        if total is None or not total.is_finite() or total < 0:
            # A hidden field: shown with the errors of the whole form.
            self.add_error(
                None,
                ValidationError(
                    _("De prijs kon niet gecontroleerd worden. Bevestig opnieuw."),
                    code="invalid_expected_total",
                ),
            )
            return None
        return total
