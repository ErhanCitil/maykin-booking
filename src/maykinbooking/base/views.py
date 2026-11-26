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
from django.db.models import Count, Min
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import translation
from django.utils.translation import gettext, gettext_lazy as _
from django.views import generic

from django_countries import countries
from formtools.wizard.views import SessionWizardView

from maykinbooking.billing.models import OPEN_PAYMENT_STATES
from maykinbooking.billing.providers import online_payment_available
from maykinbooking.billing.services.payments import (
    PaymentStartFailed,
    checkout_url_allowed,
    start_online_payment,
)
from maykinbooking.billing.services.pricing import (
    InvalidDiscountCode,
    discount_lines,
    night_lines,
)
from maykinbooking.bookings.services.availability import (
    available_hotel_ids,
    available_rooms,
)
from maykinbooking.bookings.services.booking import (
    Actor,
    OnlinePaymentUnavailable,
    PaymentMethod,
    PriceChanged,
    RoomUnavailable,
    book_room,
    quote,
)
from maykinbooking.bookings.throttle import email_limit_key, ip_over_limit, over_limit
from maykinbooking.form.forms import (
    INVALID_DISCOUNT_CODE,
    AvailabilityFilterForm,
    ConfirmForm,
    GuestForm,
    StayForm,
)

from .models import City, Highlight, Hotel, Order, OrderStatus, Room

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

# Tries of a discount code (posted in step 1) per IP address per hour; more are
# refused without looking the code up, so codes cannot be guessed.
DISCOUNT_CODE_LIMIT_PER_IP_PER_HOUR = 10
# The night list in the summary is folded away for longer stays.
SUMMARY_NIGHTS_OPEN = 7


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


def open_checkout_url(order):
    """
    The payment page of the open payment of an order that waits for payment,
    or ``None``. Only a URL that the payment service allows.
    """
    if order.status != OrderStatus.PENDING_PAYMENT:
        return None
    payment = (
        order.payments.filter(status__in=OPEN_PAYMENT_STATES)
        .exclude(checkout_url="")
        .order_by("-created_at", "-pk")
        .first()
    )
    if payment is None or not checkout_url_allowed(payment.checkout_url):
        return None
    return payment.checkout_url


class Checkout(PeriodMixin, SessionWizardView):
    """
    Three step checkout: choose your stay (with an optional discount code),
    enter your details, review the price and choose how to pay. The order is
    only created when the guest confirms in the last step.

    Paying at the hotel books the room at once. Paying online books it while it
    waits for payment, starts the payment and sends the guest to the payment
    page; the provider sends the guest back to ``billing:payment_return``.

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
        elif step == "confirm":
            kwargs["online_payment"] = self.online_payment_offered()
        return kwargs

    def get_form_initial(self, step):
        initial = super().get_form_initial(step)
        if step == "stay" and self.period:
            start, end = self.period
            initial = {**initial, "start_date": start, "end_date": end}
        return initial

    def post(self, *args, **kwargs):
        # Sent twice (double click, a replayed request): this checkout already
        # made its booking. Checked before the steps are validated again: the
        # booking itself may have taken the last room.
        existing = self.existing_order()
        if existing is not None:
            self.storage.reset()
            return self.booking_made(existing)
        if not self.bookable:
            return self.render(self.get_form())
        # formtools would fail on a step that does not exist (KeyError).
        posted_step = self.request.POST.get(f"{self.prefix}-current_step")
        if posted_step is not None and posted_step not in self.get_form_list():
            return HttpResponseBadRequest()
        return super().post(*args, **kwargs)

    def get_form(self, step=None, data=None, files=None):
        """
        The form of a step. The form of step 1 gets the discount code counter
        when it validates what the guest posts now (``data`` is the request
        data), whatever step the post says it is for: formtools validates step
        1 with the posted data when the session has no step yet. Validating the
        stored steps again (on later steps, before booking) does not count.
        """
        form = super().get_form(step=step, data=data, files=files)
        if (step or self.steps.current) == self.steps.first and (
            data is not None and data is self.request.POST
        ):
            form.count_discount_code = self.count_discount_code_attempt
        return form

    def existing_order(self):
        """The order made with the token of this checkout, or ``None``."""
        token = self.storage.extra_data.get("booking_token")
        if not token:
            return None
        try:
            token = uuid.UUID(token)
        except (TypeError, ValueError):
            return None
        return Order.objects.filter(token=token).first()

    def count_discount_code_attempt(self):
        """
        Count a discount code that is about to be looked up for the client's IP
        address (``StayForm.clean_discount_code``). Returns ``True`` above
        ``DISCOUNT_CODE_LIMIT_PER_IP_PER_HOUR``: the code is refused then,
        without looking it up.
        """
        throttled = ip_over_limit(
            self.request, "discount-code", DISCOUNT_CODE_LIMIT_PER_IP_PER_HOUR, 3600
        )
        if throttled:
            logger.info("discount code attempts throttled")
        return throttled

    def process_step(self, form):
        if self.steps.current == self.steps.first:
            self.get_booking_token()
            # The stay changes: its price is computed again.
            self.__dict__.pop("stay_offer", None)
        return super().process_step(form)

    def get_booking_token(self):
        """
        The idempotency key of this checkout: made at the first step and kept in
        the wizard storage until the booking is made. A confirmation that is
        sent twice books once (``create_booking(token=...)``) and starts one
        payment at most.
        """
        extra_data = self.storage.extra_data
        if "booking_token" not in extra_data:
            self.storage.extra_data = {**extra_data, "booking_token": uuid.uuid4().hex}
        return uuid.UUID(self.storage.extra_data["booking_token"])

    def renew_booking_token(self):
        """
        A new idempotency key, after a booking with the old one did not go
        through (its payment could not be started): confirming again books anew.
        """
        self.storage.extra_data = {
            **self.storage.extra_data,
            "booking_token": uuid.uuid4().hex,
        }

    @cached_property
    def stay_offer(self):
        """
        ``(cleaned data of step 1, Quote)`` of the stored stay, or ``None`` when
        step 1 is not (or no longer) valid.
        """
        data = self.get_cleaned_data_for_step("stay")
        if not data:
            return None
        room = data["room"]
        try:
            offer = quote(
                room,
                data["start_date"],
                data["end_date"],
                discount_code=data.get("discount_code", ""),
            )
        except InvalidDiscountCode:
            # Used up just now; confirming sends the guest back to step 1.
            offer = quote(room, data["start_date"], data["end_date"])
        return data, offer

    def use_offer(self, offer):
        """Show ``offer`` (a new price) instead of the stored stay's quote."""
        if self.stay_offer is not None:
            self.stay_offer = (self.stay_offer[0], offer)

    def online_payment_offered(self):
        """Online payment is set up and there is something to pay."""
        if self.stay_offer is None or not online_payment_available():
            return False
        return self.stay_offer[1].total > 0

    def get_stay(self):
        """The chosen stay with its price breakdown, once step 1 is completed."""
        if self.steps.current == "stay" or self.stay_offer is None:
            return None
        data, offer = self.stay_offer
        room = data["room"]
        prices = {night.price for night in offer.nights}
        nights = len(offer.nights)
        return {
            "start_date": data["start_date"],
            "end_date": data["end_date"],
            "nights": nights,
            "room": room,
            "price_per_night": room.price,
            # The price of every night, when they are all the same.
            "uniform_price": prices.pop() if len(prices) == 1 else None,
            "night_lines": night_lines(offer.nights),
            "nights_open": nights <= SUMMARY_NIGHTS_OPEN,
            "subtotal": offer.subtotal,
            # The same labels as the mails, the PDF and "Mijn boeking".
            "discount_lines": discount_lines(offer),
            "total": offer.total,
        }

    def get_context_data(self, form, **kwargs):
        context = super().get_context_data(form=form, **kwargs)
        current_index = self.steps.index
        stay = self.get_stay()
        context.update(
            {
                "hotel": self.hotel,
                "bookable": self.bookable,
                "stay": stay,
                "online_payment": stay is not None and self.online_payment_offered(),
                "hold_minutes": settings.PAYMENT_HOLD_MINUTES,
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
        Book the room, or send the guest back when that fails.

        The steps are validated again first (a room that filled up meanwhile,
        or a discount code that can no longer be used, fails there).
        ``create_booking`` checks once more after locking the room; if it
        raises, the wizard storage is still intact, so the guest gets a step
        with the entered data and a message:

        * the room or the period: step 1;
        * the discount code was used up just now: step 1, at the code;
        * the price changed: step 3 with the new price, to confirm again;
        * online payment could not be started: step 3, and the booking (which
          has expired) is forgotten, so confirming again books anew.
        """
        try:
            return super().render_done(form, **kwargs)
        except InvalidDiscountCode as error:
            logger.info("checkout discount code refused reason=%s", error.reason)
            return self.render_stay_with_code_error()
        except (RoomUnavailable, ValidationError) as error:
            return self.render_stay_with_error(error)
        except PriceChanged as changed:
            logger.info("checkout price changed")
            self.use_offer(changed.quote)
            return self.render_confirm_with_message(
                gettext(
                    "De prijs van dit verblijf is gewijzigd. Bekijk de nieuwe prijs "
                    "en bevestig opnieuw."
                )
            )
        except OnlinePaymentUnavailable as error:
            # The price dropped to 0 meanwhile, or online payment was switched
            # off: nothing was booked; the guest chooses again on step 3.
            logger.info("checkout online payment unavailable")
            self.__dict__.pop("stay_offer", None)
            return self.render_confirm_with_message(
                " ".join(
                    [
                        str(error),
                        gettext(
                            "Er is niets geboekt. Bekijk het overzicht en kies hoe "
                            "je wilt betalen."
                        ),
                    ]
                )
            )
        except PaymentStartFailed:
            logger.info("checkout online payment could not be started")
            self.renew_booking_token()
            return self.render_confirm_with_message(
                gettext(
                    "Online betalen is nu niet gelukt; er is niets geboekt en niets "
                    "betaald. Probeer het opnieuw, of kies voor betalen in het hotel."
                )
            )
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

    def get_stored_form(self, step):
        """The form of ``step`` with its stored data, validated."""
        form = self.get_form(
            step=step,
            data=self.storage.get_step_data(step),
            files=self.storage.get_step_files(step),
        )
        form.is_valid()
        return form

    def render_stay_with_message(self, message):
        """Step 1 with ``message``; the wizard storage is left as it is."""
        self.storage.current_step = self.steps.first
        stay_form = self.get_stored_form(self.steps.first)
        stay_form.add_error(None, message)
        return self.render(stay_form)

    def render_stay_with_code_error(self):
        """Step 1 with the discount code refused (once, at the code)."""
        self.storage.current_step = self.steps.first
        stay_form = self.get_stored_form(self.steps.first)
        if "discount_code" not in stay_form.errors:
            stay_form.add_error("discount_code", INVALID_DISCOUNT_CODE)
        return self.render(stay_form)

    def render_confirm_with_message(self, message):
        """Step 3 with ``message``; the wizard storage is left as it is."""
        self.storage.current_step = self.steps.last
        confirm_form = self.get_stored_form(self.steps.last)
        confirm_form.add_error(None, message)
        return self.render(confirm_form)

    def done(self, form_list, form_dict, **kwargs):
        stay = form_dict["stay"].cleaned_data
        guest = form_dict["guest"].cleaned_data
        confirm = form_dict["confirm"].cleaned_data
        token = self.get_booking_token()

        self.check_throttle(guest["email"])
        # A second request with this token (double click, a replayed request)
        # gets the order of the first one, created=False: only the request that
        # made the order starts its payment.
        result = book_room(
            token=token,
            room_id=stay["room"].pk,
            start=stay["start_date"],
            end=stay["end_date"],
            guest=guest,
            language=booking_language(),
            actor=Actor.guest(),
            payment_method=confirm["payment_method"],
            discount_code=stay.get("discount_code", ""),
            expected_total=confirm["expected_total"],
        )
        order = result.order
        if not result.created:
            return self.booking_made(order)
        if confirm["payment_method"] == PaymentMethod.ONLINE:
            # Raises PaymentStartFailed (the order has expired then).
            payment = start_online_payment(order.pk)
            # Always allowed (see start_online_payment); checked once more
            # because the guest is sent there.
            if checkout_url_allowed(payment.checkout_url):
                return redirect(payment.checkout_url)
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
        """
        Where the guest goes once the checkout has its order: "Mijn boeking",
        or the payment page of an order that still waits for its payment (a
        confirmation sent twice starts no second payment).
        """
        if order.status == OrderStatus.CONFIRMED:
            messages.success(self.request, gettext("Je boeking staat vast"))
        elif order.status == OrderStatus.PENDING_PAYMENT:
            checkout_url = open_checkout_url(order)
            if checkout_url is not None:
                return redirect(checkout_url)
            messages.info(self.request, gettext("Je boeking wacht op je betaling."))
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
