import csv
import logging
from datetime import timedelta
from functools import cached_property

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib.messages.views import SuccessMessageMixin
from django.core.exceptions import ValidationError
from django.db.models import Count, Min, OuterRef, Prefetch, ProtectedError, Q, Subquery
from django.http import Http404, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.cache import add_never_cache_headers
from django.utils.formats import date_format
from django.utils.translation import gettext as _, gettext_lazy, ngettext
from django.views import generic

from maykinbooking.base.models import (
    City,
    EmailKind,
    EmailLog,
    EmailStatus,
    Hotel,
    Order,
    OrderStatus,
    PaymentStatus,
    Room,
    RoomBlock,
    StatusField,
)
from maykinbooking.bookings.emails import my_booking_url, send_booking_email
from maykinbooking.bookings.services.availability import overbooked_nights
from maykinbooking.bookings.services.booking import (
    Actor,
    CancellationNotAllowed,
    InvalidTransition,
    can_staff_cancel,
    cancel_booking,
    create_room_block,
    resend_kind,
    stay_is_over,
)

from .forms import (
    BookingCancelForm,
    BookingFilterForm,
    CityForm,
    HotelForm,
    RoomBlockForm,
    RoomForm,
    next_hotel_id,
)

logger = logging.getLogger(__name__)

# Cells starting with one of these are formulas in Excel and LibreOffice.
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

# Nights listed in an overbooking warning; the rest is summarised.
MAX_LISTED_NIGHTS = 10

# A mail about a booking is not sent again within this time of the last one of
# the same kind, so a double click or an impatient retry does not spam the guest.
RESEND_COOLDOWN = timedelta(minutes=2)


class StaffRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    """
    Only staff users (admins and employees) can use the dashboard.
    Anonymous users are sent to the login page, other users get a 403.
    """

    login_url = reverse_lazy("login")

    def test_func(self):
        return self.request.user.is_staff

    def dispatch(self, request, *args, **kwargs):
        # The pages show guest data: never keep them in a (shared) cache.
        response = super().dispatch(request, *args, **kwargs)
        add_never_cache_headers(response)
        return response


def overbooked_warning(nights, inventory):
    """Message for the nights on which more rooms are occupied than available."""
    listed = ", ".join(
        f"{date_format(day, 'j b Y')} ({occupied})"
        for day, occupied in nights[:MAX_LISTED_NIGHTS]
    )
    if len(nights) > MAX_LISTED_NIGHTS:
        remaining = len(nights) - MAX_LISTED_NIGHTS
        listed += " " + ngettext(
            "en nog %(count)s nacht", "en nog %(count)s nachten", remaining
        ) % {"count": remaining}
    return ngettext(
        "Let op: op %(count)s nacht zijn meer kamers bezet dan de %(inventory)s "
        "kamers van dit type: %(nights)s. Bestaande boekingen blijven staan.",
        "Let op: op %(count)s nachten zijn meer kamers bezet dan de %(inventory)s "
        "kamers van dit type: %(nights)s. Bestaande boekingen blijven staan.",
        len(nights),
    ) % {"count": len(nights), "inventory": inventory, "nights": listed}


class DashboardHome(StaffRequiredMixin, generic.ListView):
    template_name = "dashboard/home.html"
    context_object_name = "hotels"
    paginate_by = 25

    def get_queryset(self):
        hotels = (
            Hotel.objects.select_related("city")
            .annotate(
                room_count=Count("room", distinct=True),
                min_price=Min("room__price"),
            )
            .order_by("city__name", "name")
        )
        query = self.request.GET.get("q", "").strip()
        if query:
            hotels = hotels.filter(name__icontains=query)
        city = self.request.GET.get("city", "")
        if city.isdigit():
            hotels = hotels.filter(city__pk=int(city))
        return hotels

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        params = self.request.GET.copy()
        params.pop("page", None)
        today = timezone.localdate()
        confirmed = Order.objects.filter(status=OrderStatus.CONFIRMED)
        context.update(
            {
                "stats": [
                    ("Hotels", Hotel.objects.count()),
                    ("Steden", City.objects.count()),
                    ("Kamers", Room.objects.count()),
                ],
                "arrivals_today": confirmed.filter(start_date=today).count(),
                "upcoming_arrivals": confirmed.filter(start_date__gte=today).count(),
                "cities": City.objects.order_by("name"),
                "query": self.request.GET.get("q", "").strip(),
                "selected_city": self.request.GET.get("city", ""),
                "querystring": params.urlencode(),
            }
        )
        return context


class HotelCreate(StaffRequiredMixin, SuccessMessageMixin, generic.CreateView):
    model = Hotel
    form_class = HotelForm
    template_name = "dashboard/hotel_form.html"
    success_message = "%(name)s is toegevoegd. Voeg nu de kamers en prijzen toe."

    def get_initial(self):
        initial = super().get_initial()
        city = self.request.GET.get("city", "")
        if city.isdigit():
            initial["city"] = int(city)
        return initial

    def form_valid(self, form):
        form.instance.hotel_id = next_hotel_id(form.instance.city)
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("dashboard:hotel_edit", args=[self.object.pk])


class HotelUpdate(StaffRequiredMixin, SuccessMessageMixin, generic.UpdateView):
    model = Hotel
    form_class = HotelForm
    template_name = "dashboard/hotel_form.html"
    success_message = "De wijzigingen in %(name)s zijn opgeslagen."

    def get_queryset(self):
        return Hotel.objects.select_related("city")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["rooms"] = self.object.room.order_by("price")
        return context

    def get_success_url(self):
        return reverse("dashboard:hotel_edit", args=[self.object.pk])


class DeleteWithMessageMixin:
    """
    Confirmation page with the consequences of deleting, and a message after.

    Orders protect their hotel and room (``on_delete=PROTECT``): when
    :meth:`get_protected_message` returns a text, the page explains why the
    object cannot be deleted instead of offering the button, and a POST
    redirects back with that text.
    """

    template_name = "dashboard/confirm_delete.html"

    def get_label(self, obj):
        return str(obj)

    def get_consequences(self):
        return []

    def get_protected_message(self):
        return None

    def get_cancel_url(self):
        raise NotImplementedError

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "label": self.get_label(self.object),
                "consequences": self.get_consequences(),
                "protected_message": self.get_protected_message(),
                "cancel_url": self.get_cancel_url(),
            }
        )
        return context

    def post(self, request, *args, **kwargs):
        # Wraps the whole POST: Django 3.2 deletes in ``delete()``, Django 4+ in
        # ``form_valid()``; both run inside ``super().post()``.
        self.object = self.get_object()
        label = self.get_label(self.object)
        protected_message = self.get_protected_message()
        if protected_message:
            messages.error(request, protected_message)
            return redirect(self.get_cancel_url())
        try:
            response = super().post(request, *args, **kwargs)
        except ProtectedError:
            # A booking came in after the check above.
            messages.error(
                request,
                self.get_protected_message()
                or _("%(label)s heeft boekingen en kan niet verwijderd worden.")
                % {"label": label},
            )
            return redirect(self.get_cancel_url())
        messages.success(request, _("%(label)s is verwijderd.") % {"label": label})
        return response


class HotelDelete(StaffRequiredMixin, DeleteWithMessageMixin, generic.DeleteView):
    model = Hotel
    success_url = reverse_lazy("dashboard:home")

    def get_consequences(self):
        rooms = self.object.room.count()
        if rooms:
            return [_("%(count)s kamer(s) worden ook verwijderd.") % {"count": rooms}]
        return []

    def get_protected_message(self):
        bookings = self.object.orders.count()
        if not bookings:
            return None
        return ngettext(
            "Dit hotel heeft %(count)s boeking en kan niet verwijderd worden. "
            "Zet het op niet beschikbaar.",
            "Dit hotel heeft %(count)s boekingen en kan niet verwijderd worden. "
            "Zet het op niet beschikbaar.",
            bookings,
        ) % {"count": bookings}

    def get_cancel_url(self):
        return reverse("dashboard:hotel_edit", args=[self.object.pk])


class RoomCreate(StaffRequiredMixin, SuccessMessageMixin, generic.CreateView):
    model = Room
    form_class = RoomForm
    template_name = "dashboard/room_form.html"
    success_message = "De kamer is toegevoegd."

    @cached_property
    def hotel(self):
        return get_object_or_404(Hotel, pk=self.kwargs["hotel_pk"])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["hotel"] = self.hotel
        return context

    def form_valid(self, form):
        form.instance.hotel = self.hotel
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("dashboard:hotel_edit", args=[self.hotel.pk])


class RoomUpdate(StaffRequiredMixin, SuccessMessageMixin, generic.UpdateView):
    model = Room
    form_class = RoomForm
    template_name = "dashboard/room_form.html"
    success_message = "De wijzigingen in de kamer zijn opgeslagen."

    def get_queryset(self):
        return Room.objects.select_related("hotel")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["hotel"] = self.object.hotel
        return context

    def form_valid(self, form):
        response = super().form_valid(form)
        # Saving always goes through: existing bookings are never cancelled.
        overbooked = overbooked_nights(self.object)
        if overbooked:
            messages.warning(
                self.request, overbooked_warning(overbooked, self.object.inventory)
            )
        return response

    def get_success_url(self):
        return reverse("dashboard:hotel_edit", args=[self.object.hotel.pk])


class RoomDelete(StaffRequiredMixin, DeleteWithMessageMixin, generic.DeleteView):
    model = Room

    def get_queryset(self):
        return Room.objects.select_related("hotel")

    def get_label(self, obj):
        return f"De kamer {obj.get_room_type_display()} van {obj.hotel.name}"

    def get_consequences(self):
        blocks = self.object.blocks.count()
        if blocks:
            return [
                _("%(count)s blokkade(s) van deze kamer worden ook verwijderd.")
                % {"count": blocks}
            ]
        return []

    def get_protected_message(self):
        bookings = self.object.orders.count()
        if not bookings:
            return None
        return ngettext(
            "Deze kamer heeft %(count)s boeking en kan niet verwijderd worden. "
            "Zet de kamer op niet beschikbaar.",
            "Deze kamer heeft %(count)s boekingen en kan niet verwijderd worden. "
            "Zet de kamer op niet beschikbaar.",
            bookings,
        ) % {"count": bookings}

    def get_cancel_url(self):
        return reverse("dashboard:room_edit", args=[self.object.pk])

    def get_success_url(self):
        return reverse("dashboard:hotel_edit", args=[self.object.hotel.pk])


class CityCreate(StaffRequiredMixin, SuccessMessageMixin, generic.CreateView):
    model = City
    form_class = CityForm
    template_name = "dashboard/city_form.html"
    success_message = "%(name)s is toegevoegd. Je kunt er nu hotels aan koppelen."

    def get_success_url(self):
        return f"{reverse('dashboard:hotel_create')}?city={self.object.pk}"


# Bookings


def search_q(query):
    """
    A booking number (``1234`` or ``#1234``) finds that booking. Other text finds
    the orders whose guest name or e-mail address contains every word.
    """
    query = query.strip().removeprefix("#")
    if query.isdecimal():
        number = int(query)
        # Larger numbers do not fit the id column (and SQLite raises on them).
        return Q(pk=number) if number < 2**31 else Q(pk__in=[])
    condition = Q()
    for word in query.split():
        condition &= (
            Q(first_name__icontains=word)
            | Q(last_name__icontains=word)
            | Q(email__icontains=word)
        )
    return condition


class BookingFilterMixin:
    """
    The orders of the booking list and the CSV export, filtered with the GET
    parameters of ``BookingFilterForm``.

    Without a search term, arrival dates or a quick filter, only the arrivals
    from today on are shown. Always sorted by arrival, oldest first.
    """

    @cached_property
    def filter_form(self):
        form = BookingFilterForm(self.request.GET)
        # Invalid fields are left out of cleaned_data, and so not applied; the
        # form shows their errors.
        form.is_valid()
        return form

    @cached_property
    def shows_default_period(self):
        data = self.filter_form.cleaned_data
        return not any(
            data.get(field) for field in ("q", "aankomst_van", "aankomst_tot", "snel")
        )

    def get_queryset(self):
        last_mail_status = (
            EmailLog.objects.filter(order=OuterRef("pk"))
            .order_by("-created_at", "-pk")
            .values("status")[:1]
        )
        orders = Order.objects.select_related("hotel__city", "room").annotate(
            last_mail_status=Subquery(last_mail_status)
        )

        data = self.filter_form.cleaned_data
        today = timezone.localdate()
        if data.get("q"):
            orders = orders.filter(search_q(data["q"]))
        if data.get("stad"):
            orders = orders.filter(hotel__city=data["stad"])
        if data.get("hotel"):
            orders = orders.filter(hotel=data["hotel"])
        if data.get("status"):
            orders = orders.filter(status=data["status"])
        if data.get("aankomst_van"):
            orders = orders.filter(start_date__gte=data["aankomst_van"])
        if data.get("aankomst_tot"):
            orders = orders.filter(start_date__lte=data["aankomst_tot"])
        if data.get("snel") == BookingFilterForm.ARRIVING_TODAY:
            orders = orders.filter(start_date=today)
        elif data.get("snel") == BookingFilterForm.LEAVING_TODAY:
            orders = orders.filter(end_date=today)
        if data.get("mail") == BookingFilterForm.MAIL_FAILED:
            orders = orders.filter(last_mail_status=EmailStatus.FAILED)
        if self.shows_default_period:
            orders = orders.filter(start_date__gte=today)
        return orders.order_by("start_date", "pk")


class BookingList(StaffRequiredMixin, BookingFilterMixin, generic.ListView):
    template_name = "dashboard/booking_list.html"
    context_object_name = "orders"
    paginate_by = 25

    def querystring(self, **changes):
        """The current GET parameters without the page, with ``changes`` applied."""
        params = self.request.GET.copy()
        params.pop("page", None)
        for key, value in changes.items():
            params.pop(key, None)
            if value:
                params[key] = value
        return params.urlencode()

    def get_quick_filters(self):
        current = self.request.GET
        filters = []
        for param, value, label in (
            ("snel", BookingFilterForm.ARRIVING_TODAY, _("Aankomsten vandaag")),
            ("snel", BookingFilterForm.LEAVING_TODAY, _("Vertrekken vandaag")),
            ("mail", BookingFilterForm.MAIL_FAILED, _("Mail mislukt")),
        ):
            active = current.get(param) == value
            # A quick filter is a toggle: clicking an active one turns it off.
            querystring = self.querystring(**{param: "" if active else value})
            filters.append({"label": label, "active": active, "query": querystring})
        return filters

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "form": self.filter_form,
                "shows_default_period": self.shows_default_period,
                "quick_filters": self.get_quick_filters(),
                "is_filtered": bool(self.querystring()),
                "querystring": self.querystring(),
                "mail_failed": EmailStatus.FAILED,
            }
        )
        return context


class Echo:
    """A file-like object for ``csv.writer`` that returns what is written."""

    def write(self, value):
        return value


def csv_cell(value):
    """
    A value as CSV text. Text that a spreadsheet would run as a formula gets a
    leading ``'`` (CSV injection: think of a guest called ``=HYPERLINK(...)``).
    """
    text = "" if value is None else str(value)
    if text.startswith(CSV_FORMULA_PREFIXES):
        return f"'{text}"
    return text


class BookingExport(StaffRequiredMixin, BookingFilterMixin, generic.View):
    """The filtered booking list as CSV for Excel: ``;``-separated, UTF-8 with BOM."""

    header = (
        gettext_lazy("Nummer"),
        gettext_lazy("Voornaam"),
        gettext_lazy("Achternaam"),
        gettext_lazy("E-mail"),
        gettext_lazy("Hotel"),
        gettext_lazy("Stad"),
        gettext_lazy("Kamer"),
        gettext_lazy("Aankomst"),
        gettext_lazy("Vertrek"),
        gettext_lazy("Nachten"),
        gettext_lazy("Totaal"),
        gettext_lazy("Status"),
        gettext_lazy("Betaalstatus"),
        gettext_lazy("Geboekt op"),
    )

    def get_row(self, order):
        created_at = (
            timezone.localtime(order.created_at).strftime("%Y-%m-%d %H:%M")
            if order.created_at
            else ""
        )
        return (
            order.pk,
            order.first_name,
            order.last_name,
            order.email,
            order.hotel.name,
            order.hotel.city.name,
            order.room.get_room_type_display(),
            order.start_date.isoformat(),
            order.end_date.isoformat(),
            order.night_count,
            # Excel with a Dutch locale expects a decimal comma next to ";".
            f"{order.total_price:.2f}".replace(".", ","),
            order.get_status_display(),
            order.get_payment_status_display(),
            created_at,
        )

    def filters_for_log(self):
        """
        The applied filters, without personal data: the search term can be a
        guest name or e-mail address, so only whether there was one is logged.
        """
        filters = []
        for name, value in sorted(self.filter_form.cleaned_data.items()):
            if value in (None, ""):
                continue
            if name == "q":
                value = "*"
            elif hasattr(value, "pk"):
                value = value.pk
            filters.append(f"{name}={value}")
        return ",".join(filters) or "-"

    def rows(self, orders):
        """
        The CSV, row by row, and a log line when the download ends: also when the
        client breaks it off halfway (the ``finally``).

        A download that is broken off before the first chunk is not logged: the
        generator never started, so closing it does not run the ``finally``. No
        guest data has left the server then.
        """
        writer = csv.writer(Echo(), delimiter=";")
        count = 0
        try:
            # The BOM makes Excel read the file as UTF-8.
            yield "\ufeff" + writer.writerow([str(column) for column in self.header])
            for order in orders.iterator(chunk_size=500):
                yield writer.writerow(
                    [csv_cell(value) for value in self.get_row(order)]
                )
                count += 1
        finally:
            # Logged when the download ends, also when it is broken off.
            logger.info(
                "booking export user=%s rows=%s filters=%s",
                self.request.user.pk,
                count,
                self.filters_for_log(),
            )

    def get(self, request, *args, **kwargs):
        response = StreamingHttpResponse(
            self.rows(self.get_queryset()), content_type="text/csv; charset=utf-8"
        )
        filename = f"boekingen-{timezone.localdate():%Y-%m-%d}.csv"
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response


RESEND_MESSAGES = {
    EmailKind.BOOKING_CONFIRMATION: (
        gettext_lazy("De bevestigingsmail is opnieuw verstuurd naar %(email)s."),
        gettext_lazy(
            "Het versturen van de bevestigingsmail is mislukt. "
            "De foutmelding staat bij de verstuurde mails."
        ),
    ),
    EmailKind.BOOKING_CANCELLATION: (
        gettext_lazy("De annuleringsmail is opnieuw verstuurd naar %(email)s."),
        gettext_lazy(
            "Het versturen van de annuleringsmail is mislukt. "
            "De foutmelding staat bij de verstuurde mails."
        ),
    ),
}


def status_label(field, value):
    choices = OrderStatus if field == StatusField.STATUS else PaymentStatus
    try:
        return choices(value).label
    except ValueError:
        return value


class BookingDetail(StaffRequiredMixin, generic.DetailView):
    template_name = "dashboard/booking_detail.html"
    context_object_name = "order"

    def get_queryset(self):
        return Order.objects.select_related("hotel__city", "room").prefetch_related(
            "nights",
            "status_changes",
            Prefetch(
                "emails", queryset=EmailLog.objects.select_related("triggered_by")
            ),
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        order = self.object
        context.update(
            {
                "status_changes": [
                    {
                        "change": change,
                        "old": status_label(change.field, change.old_value)
                        if change.old_value
                        else "",
                        "new": status_label(change.field, change.new_value),
                    }
                    for change in order.status_changes.all()
                ],
                "my_booking_url": my_booking_url(order),
                "can_cancel": can_staff_cancel(order),
                "resend_kind": resend_kind(order),
                "mail_failed": EmailStatus.FAILED,
                "mail_sent": EmailStatus.SENT,
            }
        )
        return context


class BookingCancel(StaffRequiredMixin, generic.FormView):
    """
    Staff can cancel a confirmed booking with a reason, also after the free
    cancellation deadline of the guest, until the stay is over.
    """

    form_class = BookingCancelForm
    template_name = "dashboard/booking_cancel.html"

    @cached_property
    def order(self):
        return get_object_or_404(
            Order.objects.select_related("hotel", "room"), pk=self.kwargs["pk"]
        )

    def get_detail_url(self):
        return reverse("dashboard:booking_detail", args=[self.order.pk])

    def not_cancellable(self):
        order = self.order
        if order.status == OrderStatus.CANCELLED:
            messages.info(self.request, _("Deze boeking is al geannuleerd."))
        elif order.status == OrderStatus.CONFIRMED and stay_is_over(order):
            messages.error(
                self.request,
                _(
                    "Het verblijf is voorbij; deze boeking kan niet meer geannuleerd worden."
                ),
            )
        elif order.status == OrderStatus.CONFIRMED:
            messages.error(self.request, _("Deze boeking kan niet geannuleerd worden."))
        else:
            messages.error(
                self.request,
                _("Een boeking met de status “%(status)s” kan niet geannuleerd worden.")
                % {"status": order.get_status_display()},
            )
        return redirect(self.get_detail_url())

    def get(self, request, *args, **kwargs):
        if not can_staff_cancel(self.order):
            return self.not_cancellable()
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        if not can_staff_cancel(self.order):
            return self.not_cancellable()
        return super().post(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["order"] = self.order
        return context

    def form_valid(self, form):
        try:
            order, changed = cancel_booking(
                self.order.pk,
                actor=Actor.staff(self.request.user),
                reason=form.cleaned_data["reason"],
                enforce_deadline=False,
            )
        except Order.DoesNotExist:
            raise Http404
        except (CancellationNotAllowed, InvalidTransition):
            # The status changed, or the stay ended, since the page was loaded:
            # explain with the current state of the order.
            del self.order
            return self.not_cancellable()
        if not changed:
            messages.info(self.request, _("Deze boeking is al geannuleerd."))
        else:
            messages.success(
                self.request,
                _(
                    "Boeking #%(number)s is geannuleerd. "
                    "De gast krijgt een annuleringsmail."
                )
                % {"number": order.pk},
            )
        return redirect(self.get_detail_url())


class BookingResend(StaffRequiredMixin, generic.View):
    """
    Send the last mail about the booking again (POST only): the confirmation of
    a confirmed booking, the cancellation of a cancelled one (``resend_kind``).
    Other statuses get a message and no mail, and so does a mail of the same
    kind within ``RESEND_COOLDOWN`` of the previous one that did not fail.
    """

    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        order = get_object_or_404(Order, pk=kwargs["pk"])
        detail_url = reverse("dashboard:booking_detail", args=[order.pk])
        kind = resend_kind(order)
        if kind is None:
            messages.error(
                request,
                _(
                    "Bij een boeking met de status “%(status)s” kan geen mail "
                    "opnieuw verstuurd worden."
                )
                % {"status": order.get_status_display()},
            )
            return redirect(detail_url)

        # A mail that failed does not count: sending again is the remedy for it.
        recently = timezone.now() - RESEND_COOLDOWN
        recent_mails = order.emails.filter(kind=kind, created_at__gt=recently)
        if recent_mails.exclude(status=EmailStatus.FAILED).exists():
            messages.warning(
                request,
                _(
                    "Er is net al een mail verstuurd; probeer het over een paar "
                    "minuten opnieuw."
                ),
            )
            return redirect(detail_url)

        log = send_booking_email(order.pk, kind, triggered_by=request.user)
        sent_message, failed_message = RESEND_MESSAGES[kind]
        if log is not None and log.status == EmailStatus.SENT:
            messages.success(request, sent_message % {"email": log.recipient})
        else:
            messages.error(request, failed_message)
        return redirect(detail_url)


# Room blocks


class RoomBlocks(StaffRequiredMixin, generic.FormView):
    """The current and planned blocks of a room type, and a form to add one."""

    form_class = RoomBlockForm
    template_name = "dashboard/room_blocks.html"

    @cached_property
    def room(self):
        return get_object_or_404(
            Room.objects.select_related("hotel"), pk=self.kwargs["pk"]
        )

    def get_success_url(self):
        return reverse("dashboard:room_blocks", args=[self.room.pk])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "room": self.room,
                "hotel": self.room.hotel,
                "blocks": self.room.blocks.filter(
                    end_date__gt=timezone.localdate()
                ).select_related("created_by"),
            }
        )
        return context

    def form_valid(self, form):
        data = form.cleaned_data
        try:
            block, overbooked = create_room_block(
                self.room.pk,
                start=data["start_date"],
                end=data["end_date"],
                quantity=data["quantity"],
                reason=data["reason"],
                user=self.request.user,
            )
        except Room.DoesNotExist:
            raise Http404
        except ValidationError as error:
            form.add_error(None, error)
            return self.form_invalid(form)

        messages.success(
            self.request,
            ngettext(
                "%(count)s kamer is geblokkeerd van %(start)s tot %(end)s.",
                "%(count)s kamers zijn geblokkeerd van %(start)s tot %(end)s.",
                block.quantity,
            )
            % {
                "count": block.quantity,
                "start": date_format(block.start_date, "j b Y"),
                "end": date_format(block.end_date, "j b Y"),
            },
        )
        if overbooked:
            messages.warning(
                self.request, overbooked_warning(overbooked, self.room.inventory)
            )
        return redirect(self.get_success_url())


class RoomBlockDelete(StaffRequiredMixin, generic.View):
    """Remove a block (POST only); the page asks for confirmation first."""

    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        block = get_object_or_404(RoomBlock, pk=kwargs["pk"])
        block.delete()
        messages.success(
            request,
            _("De blokkade van %(start)s tot %(end)s is verwijderd.")
            % {
                "start": date_format(block.start_date, "j b Y"),
                "end": date_format(block.end_date, "j b Y"),
            },
        )
        return redirect("dashboard:room_blocks", block.room_id)
