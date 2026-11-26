import re
import unicodedata

from django import forms
from django.utils.translation import gettext_lazy as _

from maykinbooking.base.models import City, Hotel, OrderStatus, Room

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
            "description",
            "is_available",
            "upload",
            "terms",
        )
        labels = {
            "name": "Naam",
            "address": "Adres",
            "city": "Stad",
            "description": "Beschrijving",
            "is_available": "Beschikbaar voor boekingen",
            "upload": "Foto",
        }
        help_texts = {
            "address": "Straat en huisnummer, zonder de stad.",
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
