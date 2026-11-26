import re
import unicodedata
from datetime import timedelta

from django import forms
from django.utils.translation import gettext_lazy as _

from maykinbooking.base.models import City, Hotel, OrderStatus, Room
from maykinbooking.billing.models import (
    DiscountCode,
    DiscountKind,
    InvoiceKind,
    PricingSettings,
    SeasonPeriod,
    normalize_discount_code,
)
from maykinbooking.billing.services.discounts import (
    LOCKED_FIELDS,
    LOCKED_MESSAGE,
    discount_code_is_used,
)

from .reports import MAX_REPORT_NIGHTS, month_period

INPUT_CLASS = "form-control"
CHECKBOX_CLASS = "h-5 w-5 mt-0.5 shrink-0 accent-[#13243A]"
FILE_CLASS = (
    "block w-full text-sm text-muted file:mr-4 file:min-h-[44px] file:px-5 "
    "file:rounded-full file:border-0 file:bg-ink file:text-linen file:font-semibold "
    "file:cursor-pointer hover:file:bg-[#0c1828]"
)


def next_hotel_id(city):
    """
    Generate the next hotel id for a city, following the CSV convention:
    the city id followed by a two digit sequence number (e.g. ``AMS03``).
    """
    prefix = city.city_id
    existing = Hotel.objects.filter(hotel_id__startswith=prefix).values_list(
        "hotel_id", flat=True
    )
    suffixes = (hotel_id.removeprefix(prefix) for hotel_id in existing)
    numbers = [int(suffix) for suffix in suffixes if suffix.isdigit()]
    return f"{prefix}{max(numbers, default=0) + 1:02d}"


def generate_city_id(name):
    """
    Generate a unique city id from its name, following the CSV convention of
    three capital letters (e.g. ``Rotterdam`` -> ``ROT``).
    """
    ascii_name = (
        unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    )
    base = re.sub(r"[^A-Za-z]", "", ascii_name).upper()[:3] or "CTY"
    city_id, suffix = base, 2
    while City.objects.filter(city_id=city_id).exists():
        city_id = f"{base}{suffix}"
        suffix += 1
    return city_id


class StyledFormMixin:
    """Apply the brand styling classes to every widget."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                css_class = CHECKBOX_CLASS
            elif isinstance(widget, forms.FileInput):
                css_class = FILE_CLASS
            else:
                css_class = INPUT_CLASS
            widget.attrs.setdefault("class", css_class)


class PhotoFormMixin:
    """
    Offer a checkbox to remove the uploaded photo when the instance has one.
    A newly uploaded photo always wins over the remove checkbox.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and self.instance.upload:
            self.fields["remove_upload"] = forms.BooleanField(
                required=False, label="Huidige foto verwijderen"
            )

    def save(self, commit=True):
        if self.cleaned_data.get("remove_upload") and not self.files.get("upload"):
            self.instance.upload = None
        return super().save(commit=commit)


class HotelForm(StyledFormMixin, PhotoFormMixin, forms.ModelForm):
    # Plain textarea instead of the CKEditor widget of the model field.
    terms = forms.CharField(
        label="Voorwaarden",
        required=False,
        widget=forms.Textarea(attrs={"rows": 6}),
        help_text="Wordt getoond op de voorwaardenpagina van dit hotel. HTML is toegestaan.",
    )

    class Meta:
        model = Hotel
        fields = (
            "name",
            "address",
            "city",
            "country",
            "description",
            "is_available",
            "upload",
            "terms",
        )
        labels = {
            "name": "Naam",
            "address": "Adres",
            "city": "Stad",
            "country": _("Land"),
            "description": "Beschrijving",
            "is_available": "Beschikbaar voor boekingen",
            "upload": "Foto",
        }
        help_texts = {
            "address": "Straat en huisnummer, zonder de stad.",
            "country": _(
                "Bepaalt het btw-tarief op de facturen. Zonder tarief voor dit land "
                "krijgen de facturen het watermerk “Testfactuur”."
            ),
            "is_available": "Staat dit uit, dan krijgt het hotel op de site het label “Vol”.",
            "upload": "Wordt getoond op de hotelpagina en in het stadsoverzicht.",
        }
        widgets = {
            "description": forms.Textarea(attrs={"rows": 5}),
            "upload": forms.FileInput(attrs={"accept": "image/*"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["city"].queryset = City.objects.order_by("name")
        self.fields["city"].empty_label = "Kies een stad"


class RoomForm(StyledFormMixin, PhotoFormMixin, forms.ModelForm):
    price = forms.DecimalField(
        label="Prijs per nacht (€)",
        min_value=0,
        max_digits=8,
        decimal_places=2,
        widget=forms.NumberInput(attrs={"step": "0.01"}),
    )

    class Meta:
        model = Room
        fields = (
            "room_type",
            "price",
            "inventory",
            "description",
            "is_available",
            "upload",
        )
        labels = {
            "room_type": "Kamertype",
            "inventory": _("Aantal kamers"),
            "description": "Beschrijving",
            "is_available": "Beschikbaar",
            "upload": "Foto",
        }
        help_texts = {
            "inventory": _(
                "Zoveel kamers van dit type kunnen per nacht geboekt worden. "
                "Bij 0 is de kamer niet te boeken."
            ),
        }
        widgets = {
            "description": forms.Textarea(attrs={"rows": 6}),
            "upload": forms.FileInput(attrs={"accept": "image/*"}),
        }


class CityForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = City
        fields = ("name",)
        labels = {"name": "Naam van de stad"}

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if City.objects.filter(name__iexact=name).exists():
            raise forms.ValidationError("Deze stad bestaat al.")
        return name

    def save(self, commit=True):
        city = super().save(commit=False)
        city.city_id = generate_city_id(city.name)
        if commit:
            city.save()
        return city


DATE_WIDGET = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class HotelChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, hotel):
        return f"{hotel.name} ({hotel.city.name})"


class BookingFilterForm(StyledFormMixin, forms.Form):
    """
    Filters of the booking list and the CSV export (GET). The field names are
    the query parameters. Every field is optional; see ``BookingFilterMixin``
    for how they are applied.
    """

    ARRIVING_TODAY = "aankomst_vandaag"
    LEAVING_TODAY = "vertrek_vandaag"
    MAIL_FAILED = "mislukt"
    REFUND_OPEN = "open"

    q = forms.CharField(
        label=_("Zoeken"),
        required=False,
        max_length=100,
        widget=forms.TextInput(
            attrs={"type": "search", "placeholder": _("Naam, e-mail of nr.")}
        ),
    )
    stad = forms.ModelChoiceField(
        label=_("Stad"),
        queryset=City.objects.order_by("name"),
        required=False,
        empty_label=_("Alle steden"),
    )
    hotel = HotelChoiceField(
        label=_("Hotel"),
        queryset=Hotel.objects.select_related("city").order_by("name"),
        required=False,
        empty_label=_("Alle hotels"),
    )
    status = forms.ChoiceField(
        label=_("Status"),
        choices=[("", _("Alle statussen"))] + OrderStatus.choices,
        required=False,
    )
    aankomst_van = forms.DateField(
        label=_("Aankomst vanaf"), required=False, widget=DATE_WIDGET
    )
    aankomst_tot = forms.DateField(
        label=_("Aankomst tot en met"), required=False, widget=DATE_WIDGET
    )
    snel = forms.ChoiceField(
        choices=[
            ("", ""),
            (ARRIVING_TODAY, _("Aankomsten vandaag")),
            (LEAVING_TODAY, _("Vertrekken vandaag")),
        ],
        required=False,
        widget=forms.HiddenInput,
    )
    mail = forms.ChoiceField(
        choices=[("", ""), (MAIL_FAILED, _("Mail mislukt"))],
        required=False,
        widget=forms.HiddenInput,
    )
    terugbetaling = forms.ChoiceField(
        choices=[("", ""), (REFUND_OPEN, _("Terugbetaling open"))],
        required=False,
        widget=forms.HiddenInput,
    )


class BookingCancelForm(StyledFormMixin, forms.Form):
    reason = forms.CharField(
        label=_("Reden"),
        max_length=1000,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text=_("Komt in het logboek van de boeking. De gast ziet de reden niet."),
    )
    confirm = forms.BooleanField(
        label=_("Ja, annuleer deze boeking"),
        help_text=_("De gast krijgt hierover een mail. Dit kan niet ongedaan worden."),
    )


class RoomBlockForm(StyledFormMixin, forms.Form):
    start_date = forms.DateField(label=_("Van"), widget=DATE_WIDGET)
    end_date = forms.DateField(
        label=_("Tot"),
        widget=DATE_WIDGET,
        help_text=_("Op deze datum zijn de kamers weer vrij, net als bij een vertrek."),
    )
    quantity = forms.IntegerField(
        label=_("Aantal kamers"), min_value=1, max_value=999, initial=1
    )
    reason = forms.CharField(
        label=_("Reden"),
        required=False,
        max_length=200,
        widget=forms.TextInput(attrs={"placeholder": _("Bijv. onderhoud")}),
    )

    def clean(self):
        cleaned_data = super().clean()
        start, end = cleaned_data.get("start_date"), cleaned_data.get("end_date")
        if start and end and end <= start:
            self.add_error("end_date", _("De einddatum moet na de begindatum liggen."))
        return cleaned_data


# Price rules


def percent_widget(maximum):
    return forms.NumberInput(attrs={"step": "0.01", "min": "0", "max": str(maximum)})


class PricingSettingsForm(StyledFormMixin, forms.ModelForm):
    """The price rules of the whole site; the limits are the model validators."""

    class Meta:
        model = PricingSettings
        fields = ("weekend_percent", "long_stay_min_nights", "long_stay_percent")
        labels = {
            "weekend_percent": _("Weekendtoeslag (%)"),
            "long_stay_min_nights": _("Lang verblijf vanaf (nachten)"),
            "long_stay_percent": _("Korting lang verblijf (%)"),
        }
        help_texts = {
            "weekend_percent": _(
                "Op de vrijdag- en zaterdagnacht, bovenop de kamerprijs. 0 tot 200."
            ),
            "long_stay_min_nights": _(
                "Vanaf dit aantal nachten krijgt de gast de korting. 2 tot 30."
            ),
            "long_stay_percent": _(
                "Gaat van het subtotaal van het verblijf af. 0 tot 100; "
                "bij 0 is er geen korting."
            ),
        }
        widgets = {
            "weekend_percent": percent_widget(200),
            "long_stay_min_nights": forms.NumberInput(attrs={"min": "2", "max": "30"}),
            "long_stay_percent": percent_widget(100),
        }


class SeasonForm(StyledFormMixin, forms.ModelForm):
    """
    A high season. Overlap with another season is checked when saving
    (``billing.services.pricing.save_season``), under a lock.
    """

    class Meta:
        model = SeasonPeriod
        fields = ("name", "start_date", "end_date", "percent")
        labels = {
            "name": _("Naam"),
            "start_date": _("Van"),
            "end_date": _("Tot en met"),
            "percent": _("Toeslag (%)"),
        }
        help_texts = {
            "name": _("Bijv. “Zomer 2027”. Staat bij de nacht op de factuur."),
            "end_date": _("De nacht van deze datum valt nog in het seizoen."),
            "percent": _(
                "Bovenop de kamerprijs, ook bovenop de weekendtoeslag. 0 tot 200."
            ),
        }
        widgets = {
            "start_date": DATE_WIDGET,
            "end_date": DATE_WIDGET,
            "percent": percent_widget(200),
        }

    def clean(self):
        cleaned_data = super().clean()
        start, end = cleaned_data.get("start_date"), cleaned_data.get("end_date")
        if start and end and end < start:
            self.add_error(
                "end_date", _("De einddatum mag niet voor de begindatum liggen.")
            )
        return cleaned_data


# Discount codes

# A field that cannot be changed looks like it.
LOCKED_CLASS = " !bg-linen !text-muted cursor-not-allowed"


class DiscountCodeForm(StyledFormMixin, forms.ModelForm):
    """
    The fields of a discount code, for the user: the rules are checked (and the
    code saved) by ``billing.services.discounts``, under a lock.

    After the first use code, kind and value are fixed: here they are disabled
    (a browser does not send them) and a POST that changes them anyway gets a
    message. ``changes()`` are the values for the service; never ``times_used``.
    """

    class Meta:
        model = DiscountCode
        fields = (
            "code",
            "kind",
            "value",
            "valid_from",
            "valid_until",
            "max_uses",
            "is_active",
            "description",
        )
        labels = {
            "code": _("Code"),
            "kind": _("Soort korting"),
            "value": _("Waarde"),
            "valid_from": _("Geldig van"),
            "valid_until": _("Geldig tot en met"),
            "max_uses": _("Maximaal aantal keer"),
            "is_active": _("Actief"),
            "description": _("Omschrijving"),
        }
        help_texts = {
            "code": _(
                "3 tot 40 letters, cijfers of streepjes. "
                "Wordt in hoofdletters opgeslagen."
            ),
            "value": _("Een percentage (tot en met 100) of een bedrag in euro."),
            "valid_until": _("De code werkt tot en met deze dag."),
            "max_uses": _("Leeg is onbeperkt."),
            "is_active": _("Zet dit uit om de code meteen te stoppen."),
            "description": _("Alleen voor medewerkers, bijv. waarvoor de code is."),
        }
        error_messages = {"code": {"unique": _("Deze code bestaat al.")}}
        widgets = {
            "code": forms.TextInput(
                attrs={"autocapitalize": "characters", "spellcheck": "false"}
            ),
            "value": forms.NumberInput(attrs={"step": "0.01", "min": "0.01"}),
            "valid_from": DATE_WIDGET,
            "valid_until": DATE_WIDGET,
            "max_uses": forms.NumberInput(attrs={"min": "1"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["kind"].choices = DiscountKind.choices
        self.is_locked = discount_code_is_used(self.instance)
        if self.is_locked:
            for name in LOCKED_FIELDS:
                field = self.fields[name]
                field.disabled = True
                field.widget.attrs["class"] += LOCKED_CLASS
                field.help_text = _(
                    "Ligt vast: de code is al gebruikt. Maak een nieuwe code voor "
                    "een andere korting."
                )

    def clean_code(self):
        return normalize_discount_code(self.cleaned_data.get("code"))

    def posted_locked_changes(self):
        """
        The locked fields that the POST tried to change. A browser does not send
        disabled fields, so this only happens when the form was opened before
        the first use (or the request was made by hand).
        """
        changed = []
        for name in LOCKED_FIELDS:
            raw = self.data.get(self.add_prefix(name))
            if raw is None:
                continue
            field = self.fields[name]
            try:
                value = field.to_python(raw)
            except forms.ValidationError:
                changed.append(name)
                continue
            if name == "code":
                value = normalize_discount_code(value)
            if value != getattr(self.instance, name):
                changed.append(name)
        return changed

    def clean(self):
        cleaned_data = super().clean()
        if self.is_locked and self.posted_locked_changes():
            raise forms.ValidationError(LOCKED_MESSAGE, code="locked")
        return cleaned_data

    def changes(self):
        """The values to save: the fields that can be changed."""
        return {
            name: self.cleaned_data[name]
            for name, field in self.fields.items()
            if not field.disabled and name in self.cleaned_data
        }


# Invoices


class InvoiceFilterForm(StyledFormMixin, forms.Form):
    """Filters of the invoice list and its CSV export (GET)."""

    q = forms.CharField(
        label=_("Zoeken"),
        required=False,
        max_length=100,
        widget=forms.TextInput(
            attrs={"type": "search", "placeholder": _("Nummer, naam of e-mail")}
        ),
    )
    soort = forms.ChoiceField(
        label=_("Soort"),
        choices=[("", _("Facturen en creditnota’s"))] + InvoiceKind.choices,
        required=False,
    )
    van = forms.DateField(label=_("Datum vanaf"), required=False, widget=DATE_WIDGET)
    tot = forms.DateField(
        label=_("Datum tot en met"), required=False, widget=DATE_WIDGET
    )

    def clean(self):
        cleaned_data = super().clean()
        start, end = cleaned_data.get("van"), cleaned_data.get("tot")
        if start and end and end < start:
            self.add_error("tot", _("De einddatum mag niet voor de begindatum liggen."))
        return cleaned_data


# Customers


class CustomerFilterForm(StyledFormMixin, forms.Form):
    q = forms.CharField(
        label=_("Zoeken"),
        required=False,
        max_length=100,
        widget=forms.TextInput(
            attrs={"type": "search", "placeholder": _("Naam, e-mail of boekingsnr.")}
        ),
    )


# Reports


class ReportFilterForm(StyledFormMixin, forms.Form):
    """
    The period and hotel of the report (GET). Without dates the report is of the
    current month; with one date, of the month of that date.
    """

    van = forms.DateField(label=_("Van"), required=False, widget=DATE_WIDGET)
    tot = forms.DateField(label=_("Tot en met"), required=False, widget=DATE_WIDGET)
    hotel = HotelChoiceField(
        label=_("Hotel"),
        queryset=Hotel.objects.select_related("city").order_by("name"),
        required=False,
        empty_label=_("Alle hotels"),
    )

    def clean(self):
        cleaned_data = super().clean()
        start, end = cleaned_data.get("van"), cleaned_data.get("tot")
        if start and end:
            if end < start:
                self.add_error(
                    "tot", _("De einddatum mag niet voor de begindatum liggen.")
                )
            elif (end - start).days + 1 > MAX_REPORT_NIGHTS:
                self.add_error(
                    "tot",
                    _("Kies een periode van hoogstens %(days)s dagen.")
                    % {"days": MAX_REPORT_NIGHTS},
                )
        return cleaned_data

    def period(self, default):
        """
        ``(start, end)`` of the chosen period, half-open (``end`` is the day
        after "tot en met"), or ``default`` if the dates are missing or invalid.
        """
        # Invalid fields are left out of cleaned_data; the form shows their errors.
        self.is_valid()
        start, last = self.cleaned_data.get("van"), self.cleaned_data.get("tot")
        if start and last:
            return start, last + timedelta(days=1)
        if start:
            return start, month_period(start)[1]
        if last:
            return month_period(last)[0], last + timedelta(days=1)
        return default
