import base64
import io
import logging
import uuid
from functools import cached_property
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.views import LoginView
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError
from django.db.models import Count, Min
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import translation
from django.utils.translation import gettext, gettext_lazy as _
from django.views import generic

from django_countries import countries
from formtools.wizard.views import SessionWizardView

from maykinbooking.bookings.services.availability import (
    available_hotel_ids,
    available_rooms,
)
from maykinbooking.bookings.services.booking import (
    Actor,
    RoomUnavailable,
    create_booking,
    quote,
)
from maykinbooking.bookings.throttle import email_limit_key, ip_over_limit, over_limit
from maykinbooking.form.forms import (
    AvailabilityFilterForm,
    ConfirmForm,
    GuestForm,
    StayForm,
)

from .models import City, Highlight, Hotel, Order, Room

# Create your views here.


class Index(generic.ListView):
    model = City
    template_name = "index.html"
    context_object_name = "data"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["data"] = (
            City.objects.values("name")
            .annotate(hotel_count=Count("test"))
            .order_by("name")
        )
        return context


class PeriodMixin:
    """
    The optional period of a stay in the querystring (``?van=&tot=``), so the
    city page, the hotel page and the checkout can pass it on to each other.
    """

    @cached_property
    def period_form(self):
        data = self.request.GET
        if "van" in data or "tot" in data:
            return AvailabilityFilterForm(data)
        return AvailabilityFilterForm()

    @property
    def period(self):
        """``(start, end)`` of a valid period in the querystring, or ``None``."""
        return self.period_form.period

    @property
    def period_querystring(self):
        """``van=...&tot=...`` of a valid period, or an empty string."""
        if not self.period:
            return ""
        start, end = self.period
        return urlencode({"van": start.isoformat(), "tot": end.isoformat()})

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "period": self.period,
                "period_querystring": self.period_querystring,
            }
        )
        return context


# Class-Based View voor de stad pagina ik geef de stad naam mee als parameter. Altijd met een hoofdletter de naam van een class
class Stad(PeriodMixin, generic.ListView):
    """
    The hotels of a city, optionally only those whose name matches ``?q=`` and
    those with a free room for the period ``?van=&tot=``.

    Ik filter hier op de stad naam die ik meegeef als parameter binnen de url en
    die gebruik ik dan voor de paginate. Eerst filterde ik op alle steden binnen
    in de database, en toen kreeg ik alle hotels van alle steden in de pagina te
    zien wat niet de bedoeling is.
    """

    paginate_by = 12
    model = Hotel
    template_name = "stad.html"

    @property
    def search(self):
        return self.request.GET.get("q", "").strip()

    def get_queryset(self):
        hotels = Hotel.objects.filter(city__name=self.kwargs["city_name"])
        if self.search:
            hotels = hotels.filter(name__icontains=self.search)
        hotels = hotels.annotate(min_price=Min("room__price")).order_by("name")
        if self.period:
            # A fixed number of queries, whatever the number of hotels.
            hotels = hotels.filter(pk__in=available_hotel_ids(hotels, *self.period))
        return hotels

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Kept in the links to the other pages of the list; only valid values.
        params = {"q": self.search} if self.search else {}
        if self.period:
            start, end = self.period
            params.update(van=start.isoformat(), tot=end.isoformat())
        context.update(
            {
                "city_name": self.kwargs["city_name"],
                "search": self.search,
                "filter_form": self.period_form,
                "querystring": urlencode(params),
            }
        )
        return context


class HotelList(generic.ListView):
    model = City
    template_name = "hotels.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["city_name"] = (
            City.objects.values("name")
            .annotate(hotel_count=Count("test"))
            .order_by("name")
        )
        return context


class HotelDetail(PeriodMixin, generic.DetailView):
    model = Hotel
    template_name = "hotel.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["rooms"] = Room.objects.filter(hotel=self.kwargs["pk"])
        context["min_price"] = (
            context["rooms"].aggregate(p=Min("price"))["p"] or self.object.price
        )
        context["highlights"] = Highlight.objects.filter(hotel=self.kwargs["pk"])
        if self.period:
            # Which rooms are free for the period the guest chose on the city page.
            free = available_rooms(context["rooms"], *self.period)
            context["free_room_ids"] = set(free.values_list("pk", flat=True))
        return context


class DatabaseSchema(generic.TemplateView):
    template_name = "database_schema.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        with io.StringIO() as out:
            call_command("graph_models", "-a", "-o", "static/schema.png", stdout=out)
            with open("static/schema.png", "rb") as image_file:
                encoded_string = base64.b64encode(image_file.read())
                context["schema"] = encoded_string
        return context


CHECKOUT_STEPS = [
    ("stay", _("Je verblijf")),
    ("guest", _("Je gegevens")),
    ("confirm", _("Controleren")),
]


logger = logging.getLogger(__name__)


class BookingThrottled(Exception):
    """
    Too many bookings from this IP address or for this e-mail address.
    ``reasons``: the limits that were exceeded, ``"ip"`` and/or ``"email"``.
    """

    def __init__(self, reasons):
        super().__init__(*reasons)
        self.reasons = reasons


def booking_language():
    """
    The language of the current request as stored on an order: a supported
    language code ("nl"), not the regional variant of ``LANGUAGE_CODE`` ("nl-nl").
    """
    language = translation.get_language() or settings.LANGUAGE_CODE
    try:
        return translation.get_supported_language_variant(language)
    except LookupError:
        return language.split("-")[0].lower()


class Checkout(PeriodMixin, SessionWizardView):
    """
    Three step checkout: choose your stay, enter your details, review and confirm.
    The order is only created when the guest confirms in the last step.

    ``?van=&tot=`` (from the city or hotel page) fills in the dates of step 1.
    """

    template_name = "order.html"
    form_list = [
        ("stay", StayForm),
        ("guest", GuestForm),
        ("confirm", ConfirmForm),
    ]

    @cached_property
    def hotel(self):
        return get_object_or_404(
            Hotel.objects.select_related("city"), pk=self.kwargs["pk"]
        )

    @property
    def bookable(self):
        return (
            self.hotel.is_available
            and self.hotel.room.filter(is_available=True, inventory__gt=0).exists()
        )

    def get_form_kwargs(self, step=None):
        kwargs = super().get_form_kwargs(step)
        if step == "stay":
            kwargs["hotel"] = self.hotel
        return kwargs

    def get_form_initial(self, step):
        initial = super().get_form_initial(step)
        if step == "stay" and self.period:
            start, end = self.period
            initial = {**initial, "start_date": start, "end_date": end}
        return initial

    def post(self, *args, **kwargs):
        if not self.bookable:
            return self.render(self.get_form())
        return super().post(*args, **kwargs)

    def process_step(self, form):
        if self.steps.current == self.steps.first:
            self.get_booking_token()
        return super().process_step(form)

    def get_booking_token(self):
        """
        The idempotency key of this checkout: made at the first step and kept in
        the wizard storage until the booking is made. A confirmation that is
        sent twice books once (``create_booking(token=...)``).
        """
        extra_data = self.storage.extra_data
        if "booking_token" not in extra_data:
            self.storage.extra_data = {**extra_data, "booking_token": uuid.uuid4().hex}
        return uuid.UUID(self.storage.extra_data["booking_token"])

    def get_stay(self):
        """The chosen stay with its price breakdown, once step 1 is completed."""
        if self.steps.current == "stay":
            return None
        data = self.get_cleaned_data_for_step("stay")
        if not data:
            return None
        room = data["room"]
        offer = quote(room, data["start_date"], data["end_date"])
        return {
            "start_date": data["start_date"],
            "end_date": data["end_date"],
            "nights": len(offer.nights),
            "room": room,
            "price_per_night": room.price,
            "total": offer.total,
        }

    def get_context_data(self, form, **kwargs):
        context = super().get_context_data(form=form, **kwargs)
        current_index = self.steps.index
        context.update(
            {
                "hotel": self.hotel,
                "bookable": self.bookable,
                "stay": self.get_stay(),
                "checkout_steps": [
                    {
                        "number": index + 1,
                        "label": label,
                        "done": index < current_index,
                        "current": index == current_index,
                    }
                    for index, (name, label) in enumerate(CHECKOUT_STEPS)
                ],
            }
        )
        if self.steps.current == "confirm":
            guest = self.get_cleaned_data_for_step("guest") or {}
            context["guest"] = {
                **guest,
                "country_name": countries.name(guest.get("country", "")),
            }
        return context

    def render_done(self, form, **kwargs):
        """
        Book the room, or send the guest back to step 1 when that fails.

        The steps are validated again first (a room that filled up meanwhile
        fails there). ``create_booking`` checks once more after locking the room;
        if it raises, the wizard storage is still intact, so step 1 is shown
        with the entered data and the guest details are kept.
        """
        try:
            return super().render_done(form, **kwargs)
        except (RoomUnavailable, ValidationError) as error:
            return self.render_stay_with_error(error)
        except BookingThrottled as throttled:
            # Which limit, never the IP or e-mail address itself.
            logger.info("checkout throttled reason=%s", ",".join(throttled.reasons))
            return self.render_stay_with_message(
                gettext(
                    "Er zijn vanaf dit adres of voor dit e-mailadres al veel "
                    "boekingen gemaakt. Probeer het later opnieuw, of mail naar "
                    "%(contact_email)s."
                )
                % {"contact_email": settings.BOOKING_CONTACT_EMAIL}
            )

    def render_stay_with_error(self, error):
        if isinstance(error, ValidationError):
            reasons = error.messages
        else:
            reasons = [str(error)] if str(error) else []
        return self.render_stay_with_message(
            " ".join(
                [gettext("Het boeken is niet gelukt."), *reasons]
                + [gettext("Kies andere data of een andere kamer.")]
            )
        )

    def render_stay_with_message(self, message):
        """Step 1 with ``message``; the wizard storage is left as it is."""
        self.storage.current_step = self.steps.first
        stay_form = self.get_form(
            step=self.steps.first,
            data=self.storage.get_step_data(self.steps.first),
            files=self.storage.get_step_files(self.steps.first),
        )
        stay_form.is_valid()
        stay_form.add_error(None, message)
        return self.render(stay_form)

    def done(self, form_list, form_dict, **kwargs):
        stay = form_dict["stay"].cleaned_data
        guest = form_dict["guest"].cleaned_data
        token = self.get_booking_token()

        # Sent twice (double click, a replayed request): this checkout already
        # made its booking.
        existing = Order.objects.filter(token=token).first()
        if existing is not None:
            return self.booking_made(existing)

        self.check_throttle(guest["email"])
        try:
            order = create_booking(
                room_id=stay["room"].pk,
                start=stay["start_date"],
                end=stay["end_date"],
                guest=guest,
                language=booking_language(),
                actor=Actor.guest(),
                token=token,
            )
        except IntegrityError:
            # The other request won the race to the unique token.
            existing = Order.objects.filter(token=token).first()
            if existing is None:
                raise
            order = existing
        return self.booking_made(order)

    def check_throttle(self, email):
        """
        Raise ``BookingThrottled`` above the booking limits (see settings). Both
        limits count this attempt. Without a client IP address only the e-mail
        limit applies (see ``ip_over_limit``).
        """
        exceeded = []
        if ip_over_limit(
            self.request, "booking", settings.BOOKING_LIMIT_PER_IP_PER_HOUR, 3600
        ):
            exceeded.append("ip")
        if over_limit(
            f"booking:email:{email_limit_key(email)}",
            settings.BOOKING_LIMIT_PER_EMAIL_PER_DAY,
            24 * 3600,
        ):
            exceeded.append("email")
        if exceeded:
            raise BookingThrottled(exceeded)

    def booking_made(self, order):
        messages.success(self.request, gettext("Je boeking staat vast"))
        return redirect("bookings:detail", token=order.token)


class Login(LoginView):
    template_name = "login.html"
    fields = "__all__"
    redirect_authenticated_user = True

    def get_success_url(self):
        # Staff members go straight to the dashboard, unless ?next= says otherwise.
        if self.get_redirect_url():
            return self.get_redirect_url()
        if self.request.user.is_staff:
            return reverse("dashboard:home")
        return reverse("index")


class Terms(generic.TemplateView):
    template_name = "terms.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["terms"] = Hotel.objects.get(id=self.kwargs["pk"])
        return context
