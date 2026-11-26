import csv
import logging
from datetime import date, timedelta
from decimal import Decimal
from functools import cached_property
from itertools import islice

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.contrib.messages.views import SuccessMessageMixin
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db.models import (
    Count,
    Exists,
    Min,
    OuterRef,
    Prefetch,
    ProtectedError,
    Q,
    Subquery,
    Sum,
)
from django.http import Http404, HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.cache import add_never_cache_headers
from django.utils.formats import date_format
from django.utils.http import urlencode
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
from maykinbooking.billing.models import (
    CLOSED_REFUND_STATES,
    IN_PROGRESS_REFUND_STATES,
    DiscountCode,
    DiscountKind,
    Invoice,
    InvoiceKind,
    PaymentState,
    PricingSettings,
    Refund,
    RefundState,
    SeasonPeriod,
)
from maykinbooking.billing.services.discounts import (
    create_discount_code,
    update_discount_code,
)
from maykinbooking.billing.services.invoicing import invoice_pdf_bytes
from maykinbooking.billing.services.payments import retry_refund
from maykinbooking.billing.services.pricing import (
    PricingRules,
    length_discount,
    money,
    night_price,
    save_season,
)
from maykinbooking.bookings.emails import (
    discount_lines,
    my_booking_url,
    send_booking_email,
)
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
from maykinbooking.utils.templatetags.money import format_euro

from .customers import (
    DEFAULT_SORT,
    attach_names,
    customer_orders,
    customer_summary,
    customers,
    search_q,
    valid_sort,
)
from .forms import (
    BookingCancelForm,
    BookingFilterForm,
    CityForm,
    CustomerFilterForm,
    DiscountCodeForm,
    HotelForm,
    InvoiceFilterForm,
    PricingSettingsForm,
    ReportFilterForm,
    RoomBlockForm,
    RoomForm,
    SeasonForm,
    next_hotel_id,
)
from .reports import current_month, hotel_report, report_total, revenue_total

logger = logging.getLogger(__name__)

# Cells starting with one of these are formulas in Excel and LibreOffice.
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

# Nights listed in an overbooking warning; the rest is summarised.
MAX_LISTED_NIGHTS = 10

# A mail about a booking is not sent again within this time of the last one of
# the same kind, so a double click or an impatient retry does not spam the guest.
RESEND_COOLDOWN = timedelta(minutes=2)

# Refunds that still have to reach the guest: not sent yet, failed to send, or
# on their way at the provider.
OPEN_REFUND_STATES = (RefundState.REQUESTED, RefundState.ERROR) + tuple(
    IN_PROGRESS_REFUND_STATES
)

# The part of the dashboard a page belongs to, for the navigation (by URL name).
NAV_SECTIONS = {
    "bookings": ("booking_list", "booking_detail", "booking_cancel"),
    "hotels": (
        "home",
        "hotel_create",
        "hotel_edit",
        "hotel_delete",
        "room_create",
        "room_edit",
        "room_delete",
        "room_blocks",
        "city_create",
    ),
    "pricing": ("pricing_rules", "season_create", "season_edit"),
    "discounts": ("discount_list", "discount_create", "discount_edit"),
    "invoices": ("invoice_list",),
    "customers": ("customer_list", "customer_detail"),
    "reports": ("report",),
}


def nav_section(url_name):
    for section, names in NAV_SECTIONS.items():
        if url_name in names:
            return section
    return ""


class StaffRequiredMixin(LoginRequiredMixin, PermissionRequiredMixin):
    """
    Only active staff users with the permissions of the view can use it.
    Anonymous users are sent to the login page, other users get a 403.

    Every view sets ``permission_required`` (``()``: staff is enough); a view
    without it raises ``ImproperlyConfigured``. The roles are groups with
    permissions (``accounts.roles``); the superuser has every permission.
    """

    login_url = reverse_lazy("login")
    permission_required = None

    def get_permission_required(self):
        if self.permission_required is None:
            raise ImproperlyConfigured(
                f"{type(self).__name__} must set permission_required "
                "(an empty tuple if staff is enough)."
            )
        if isinstance(self.permission_required, str):
            return (self.permission_required,)
        return tuple(self.permission_required)

    def has_permission(self):
        user = self.request.user
        return (
            user.is_active
            and user.is_staff
            and user.has_perms(self.get_permission_required())
        )

    def dispatch(self, request, *args, **kwargs):
        # The pages show guest data: never keep them in a (shared) cache.
        response = super().dispatch(request, *args, **kwargs)
        add_never_cache_headers(response)
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        match = self.request.resolver_match
        context["nav_section"] = nav_section(match.url_name if match else "")
        return context


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
    """
    The hotels. Every staff member can open it; who cannot see hotels but can
    see bookings (the reception) goes to the bookings.
    """

    permission_required = ()
    template_name = "dashboard/home.html"
    context_object_name = "hotels"
    paginate_by = 25

    def get(self, request, *args, **kwargs):
        user = request.user
        if not user.has_perm("base.view_hotel") and user.has_perm("base.view_order"):
            return redirect("dashboard:booking_list")
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        if not self.request.user.has_perm("base.view_hotel"):
            # Staff without a role: the page only says so.
            return Hotel.objects.none()
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
        user = self.request.user
        if not user.has_perm("base.view_hotel"):
            return context
        params = self.request.GET.copy()
        params.pop("page", None)
        today = timezone.localdate()
        context.update(
            {
                "stats": [
                    ("Hotels", Hotel.objects.count()),
                    ("Steden", City.objects.count()),
                    ("Kamers", Room.objects.count()),
                ],
                "cities": City.objects.order_by("name"),
                "query": self.request.GET.get("q", "").strip(),
                "selected_city": self.request.GET.get("city", ""),
                "querystring": params.urlencode(),
            }
        )
        if user.has_perm("base.view_order"):
            confirmed = Order.objects.filter(status=OrderStatus.CONFIRMED)
            open_refunds = Refund.objects.filter(status__in=OPEN_REFUND_STATES)
            context.update(
                {
                    "arrivals_today": confirmed.filter(start_date=today).count(),
                    "upcoming_arrivals": confirmed.filter(
                        start_date__gte=today
                    ).count(),
                    "open_refunds": open_refunds.values("payment__order")
                    .distinct()
                    .count(),
                    "failed_refunds": open_refunds.filter(
                        status=RefundState.ERROR
                    ).count(),
                }
            )
        if user.has_perm("base.view_reports"):
            start, end = current_month(today)
            month = revenue_total(start, end)
            context.update(
                {
                    "month_revenue": month.revenue,
                    # Invoices without a VAT rate: not in the revenue.
                    "month_untaxed_invoices": month.untaxed_invoices,
                    "month_untaxed_incl": month.untaxed_incl,
                }
            )
            context["month_start"] = start
        return context


class HotelCreate(StaffRequiredMixin, SuccessMessageMixin, generic.CreateView):
    permission_required = "base.add_hotel"
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
    permission_required = "base.change_hotel"
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
    permission_required = "base.delete_hotel"
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
    permission_required = "base.add_room"
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
    permission_required = "base.change_room"
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
    permission_required = "base.delete_room"
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
    permission_required = "base.add_city"
    model = City
    form_class = CityForm
    template_name = "dashboard/city_form.html"
    success_message = "%(name)s is toegevoegd. Je kunt er nu hotels aan koppelen."

    def get_success_url(self):
        return f"{reverse('dashboard:hotel_create')}?city={self.object.pk}"


# Bookings


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
            data.get(field)
            for field in ("q", "aankomst_van", "aankomst_tot", "snel", "terugbetaling")
        )

    def get_queryset(self):
        last_mail_status = (
            EmailLog.objects.filter(order=OuterRef("pk"))
            .order_by("-created_at", "-pk")
            .values("status")[:1]
        )
        refunds = Refund.objects.filter(payment__order=OuterRef("pk"))
        orders = Order.objects.select_related("hotel__city", "room").annotate(
            last_mail_status=Subquery(last_mail_status),
            refund_failed=Exists(refunds.filter(status=RefundState.ERROR)),
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
        if data.get("terugbetaling") == BookingFilterForm.REFUND_OPEN:
            orders = orders.filter(
                Exists(refunds.filter(status__in=OPEN_REFUND_STATES))
            )
        if self.shows_default_period:
            orders = orders.filter(start_date__gte=today)
        return orders.order_by("start_date", "pk")


class BookingList(StaffRequiredMixin, BookingFilterMixin, generic.ListView):
    permission_required = "base.view_order"
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
            ("terugbetaling", BookingFilterForm.REFUND_OPEN, _("Terugbetaling open")),
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


class CsvAmount(str):
    """An amount formatted by :func:`csv_amount`: never a formula, also when negative."""


def csv_cell(value):
    """
    A value as CSV text. Text that a spreadsheet would run as a formula gets a
    leading ``'`` (CSV injection: think of a guest called ``=HYPERLINK(...)``).
    Amounts that we formatted ourselves stay numbers, so a credit note of
    ``-242,00`` can still be added up.
    """
    text = "" if value is None else str(value)
    if isinstance(value, CsvAmount):
        return text
    if text.startswith(CSV_FORMULA_PREFIXES):
        return f"'{text}"
    return text


def csv_amount(amount):
    """An amount for Excel with a Dutch locale: a decimal comma next to ``;``."""
    return CsvAmount(f"{Decimal(amount):.2f}".replace(".", ","))


def filters_for_log(form, *, hidden=("q",)):
    """
    The applied filters of ``form``, without personal data: a search term can be
    a guest name or e-mail address, so only whether there was one is logged.
    """
    filters = []
    for name, value in sorted(form.cleaned_data.items()):
        if value in (None, ""):
            continue
        if name in hidden:
            value = "*"
        elif hasattr(value, "pk"):
            value = value.pk
        filters.append(f"{name}={value}")
    return ",".join(filters) or "-"


class CsvExportMixin:
    """
    A download as CSV for Excel: ``;``-separated, UTF-8 with BOM, every cell
    through :func:`csv_cell`. Streamed, and logged (who, how many rows, which
    filters; never guest data) when the download ends.

    Subclasses set ``header``, ``export_name`` (for the log and the file name)
    and implement ``get_items`` and ``get_row``.
    """

    header = ()
    export_name = ""
    filename_prefix = ""

    def get_items(self):
        raise NotImplementedError

    def get_row(self, item):
        raise NotImplementedError

    def filters_for_log(self):
        return "-"

    def rows(self, items):
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
            for item in items:
                yield writer.writerow([csv_cell(value) for value in self.get_row(item)])
                count += 1
        finally:
            # Logged when the download ends, also when it is broken off.
            logger.info(
                "%s export user=%s rows=%s filters=%s",
                self.export_name,
                self.request.user.pk,
                count,
                self.filters_for_log(),
            )

    def get_filename(self):
        return f"{self.filename_prefix}-{timezone.localdate():%Y-%m-%d}.csv"

    def get(self, request, *args, **kwargs):
        response = StreamingHttpResponse(
            self.rows(self.get_items()), content_type="text/csv; charset=utf-8"
        )
        response[
            "Content-Disposition"
        ] = f'attachment; filename="{self.get_filename()}"'
        return response


class BookingExport(
    StaffRequiredMixin, BookingFilterMixin, CsvExportMixin, generic.View
):
    """The filtered booking list as CSV for Excel: ``;``-separated, UTF-8 with BOM."""

    permission_required = "base.view_order"
    export_name = "booking"
    filename_prefix = "boekingen"
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
            csv_amount(order.total_price),
            order.get_status_display(),
            order.get_payment_status_display(),
            created_at,
        )

    def filters_for_log(self):
        return filters_for_log(self.filter_form)

    def get_items(self):
        return self.get_queryset().iterator(chunk_size=500)


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


def refund_retry_needed(order, refunds):
    """
    Whether staff should try a refund again: one failed to reach the provider
    (``error``), or the provider refused the last one while the order is
    cancelled (or expired) and still paid. See ``retry_refund``.
    """
    if any(refund.status == RefundState.ERROR for refund in refunds):
        return True
    return (
        order.payment_status == PaymentStatus.PAID
        and order.status in (OrderStatus.CANCELLED, OrderStatus.EXPIRED)
        and not any(refund.status not in CLOSED_REFUND_STATES for refund in refunds)
    )


def payment_panel(order):
    """
    The payment, refunds and invoices of an order for the booking page, from
    the prefetched ``payments__refunds`` and ``invoices``.
    """
    payments = sorted(order.payments.all(), key=lambda p: (p.created_at, p.pk))
    paid = [payment for payment in payments if payment.status == PaymentState.PAID]
    refunds = sorted(
        (refund for payment in payments for refund in payment.refunds.all()),
        key=lambda refund: (refund.created_at, refund.pk),
    )
    invoices = {invoice.kind: invoice for invoice in order.invoices.all()}
    return {
        "payments": payments,
        "payment": (paid[0] if paid else payments[-1]) if payments else None,
        "refunds": refunds,
        "refund_retry_needed": refund_retry_needed(order, refunds),
        "invoice": invoices.get(InvoiceKind.INVOICE),
        "credit_note": invoices.get(InvoiceKind.CREDIT_NOTE),
        "refund_error": RefundState.ERROR,
        "refund_done": RefundState.REFUNDED,
    }


class BookingDetail(StaffRequiredMixin, generic.DetailView):
    permission_required = "base.view_order"
    template_name = "dashboard/booking_detail.html"
    context_object_name = "order"

    def get_queryset(self):
        return Order.objects.select_related("hotel__city", "room").prefetch_related(
            "nights",
            "status_changes",
            Prefetch(
                "emails", queryset=EmailLog.objects.select_related("triggered_by")
            ),
            "payments__refunds__created_by",
            "invoices",
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        order = self.object
        context.update(payment_panel(order))
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
                "discount_lines": discount_lines(order),
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

    permission_required = "base.cancel_order"
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

    permission_required = "base.resend_order_email"
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

    permission_required = "base.add_roomblock"
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

    permission_required = "base.delete_roomblock"
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


class BookingRefundRetry(StaffRequiredMixin, generic.View):
    """
    Try the refund of a booking again (POST only): the refund that failed to
    reach the provider, or a new one when the provider refused the last one.
    It is sent after the commit; the message tells how it went.
    """

    permission_required = "base.cancel_order"
    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        order = get_object_or_404(Order, pk=kwargs["pk"])
        detail_url = reverse("dashboard:booking_detail", args=[order.pk])
        attempts_before = dict(
            Refund.objects.filter(payment__order=order).values_list("pk", "attempts")
        )
        try:
            refund = retry_refund(order.pk, actor=Actor.staff(request.user))
        except Order.DoesNotExist:
            raise Http404
        if refund is None:
            messages.info(
                request, _("Er hoeft voor deze boeking niets teruggestort te worden.")
            )
            return redirect(detail_url)

        logger.info(
            "refund retry user=%s order=%s refund=%s status=%s",
            request.user.pk,
            order.pk,
            refund.pk,
            refund.status,
        )
        tried = refund.attempts > attempts_before.get(refund.pk, 0)
        if refund.status == RefundState.REFUNDED:
            messages.success(
                request,
                _("%(amount)s is teruggestort naar de gast.")
                % {"amount": format_euro(refund.amount)},
            )
        elif refund.status == RefundState.ERROR and tried:
            messages.error(
                request,
                _(
                    "De terugbetaling is opnieuw mislukt. De foutmelding staat bij "
                    "de betaling."
                ),
            )
        else:
            messages.info(
                request,
                _(
                    "De terugbetaling van %(amount)s is aangevraagd. De status "
                    "staat bij de betaling."
                )
                % {"amount": format_euro(refund.amount)},
            )
        return redirect(detail_url)


# Price rules


def pricing_example(rules_row, season=None):
    """
    A worked example with the current rules, for the explanation on the page: a
    room of € 100 from Thursday to Sunday, a Saturday in ``season``, and the
    discount for a long stay.
    """
    base = Decimal("100.00")
    rules = PricingRules(
        weekend_percent=rules_row.weekend_percent,
        long_stay_min_nights=rules_row.long_stay_min_nights,
        long_stay_percent=rules_row.long_stay_percent,
    )
    # Any Thursday will do: without seasons only the day of the week counts.
    thursday = date(2026, 1, 1)
    nights = [night_price(base, thursday + timedelta(days=n), rules) for n in range(3)]
    example = {
        "base": base,
        "nights": nights,
        "total": sum((night.price for night in nights), Decimal("0.00")),
    }
    if season is not None:
        saturday = nights[2]
        example["season"] = {
            "name": season.name,
            "percent": season.percent,
            "price": money(
                base * (1 + saturday.weekend_percent / 100) * (1 + season.percent / 100)
            ),
        }
    count = rules.long_stay_min_nights
    subtotal = base * count
    percent, discount = length_discount(subtotal, count, rules)
    example["long_stay"] = {
        "nights": count,
        "subtotal": subtotal,
        "percent": percent,
        "discount": discount,
        "total": subtotal - discount,
    }
    return example


class PricingRulesView(StaffRequiredMixin, generic.UpdateView):
    """The price rules of the whole site, and the high seasons."""

    permission_required = "billing.change_pricingsettings"
    form_class = PricingSettingsForm
    template_name = "dashboard/pricing_rules.html"
    success_url = reverse_lazy("dashboard:pricing_rules")

    def get_object(self, queryset=None):
        return PricingSettings.load()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        today = timezone.localdate()
        seasons = list(SeasonPeriod.objects.order_by("start_date", "pk"))
        upcoming = [season for season in seasons if season.end_date >= today]
        saved = PricingSettings.objects.select_related("updated_by").get(pk=1)
        context.update(
            {
                "seasons": seasons,
                "today": today,
                "saved": saved,
                "example": pricing_example(
                    saved,
                    upcoming[0] if upcoming else (seasons[-1] if seasons else None),
                ),
            }
        )
        return context

    def form_valid(self, form):
        form.instance.updated_by = self.request.user
        response = super().form_valid(form)
        rules = self.object
        logger.info(
            "pricing rules changed user=%s weekend=%s long_stay_min_nights=%s "
            "long_stay=%s",
            self.request.user.pk,
            rules.weekend_percent,
            rules.long_stay_min_nights,
            rules.long_stay_percent,
        )
        messages.success(
            self.request,
            _(
                "De prijsregels zijn opgeslagen. Ze gelden voor nieuwe boekingen; "
                "bestaande boekingen houden hun prijs."
            ),
        )
        return response


class SeasonFormMixin:
    model = SeasonPeriod
    form_class = SeasonForm
    template_name = "dashboard/season_form.html"
    success_url = reverse_lazy("dashboard:pricing_rules")
    success_text = ""

    def form_valid(self, form):
        try:
            self.object = save_season(form.save(commit=False))
        except ValidationError as error:
            # Overlap with another season concerns the whole period.
            field = "end_date" if error.code == "end_before_start" else None
            form.add_error(field, error)
            return self.form_invalid(form)
        logger.info(
            "season saved user=%s season=%s", self.request.user.pk, self.object.pk
        )
        messages.success(self.request, self.success_text % {"name": self.object.name})
        return redirect(self.success_url)


class SeasonCreate(StaffRequiredMixin, SeasonFormMixin, generic.CreateView):
    permission_required = "billing.add_seasonperiod"
    success_text = gettext_lazy(
        "Het seizoen “%(name)s” is toegevoegd. Het geldt voor nieuwe boekingen."
    )


class SeasonUpdate(StaffRequiredMixin, SeasonFormMixin, generic.UpdateView):
    permission_required = "billing.change_seasonperiod"
    success_text = gettext_lazy(
        "De wijzigingen in “%(name)s” zijn opgeslagen. Ze gelden voor nieuwe "
        "boekingen."
    )


class SeasonDelete(StaffRequiredMixin, generic.View):
    """Remove a season (POST only); the page asks for confirmation first."""

    permission_required = "billing.delete_seasonperiod"
    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        season = get_object_or_404(SeasonPeriod, pk=kwargs["pk"])
        season.delete()
        logger.info("season deleted user=%s season=%s", request.user.pk, kwargs["pk"])
        messages.success(
            request,
            _(
                "Het seizoen “%(name)s” is verwijderd. Bestaande boekingen houden "
                "hun prijs."
            )
            % {"name": season.name},
        )
        return redirect("dashboard:pricing_rules")


# Discount codes


def discount_status(code, today):
    """``(label, tone)`` of the state of ``code`` today, for the badge."""
    if not code.is_active:
        return _("Uitgezet"), "neutral"
    if today < code.valid_from:
        return _("Nog niet geldig"), "warning"
    if today > code.valid_until:
        return _("Verlopen"), "neutral"
    if code.max_uses is not None and code.times_used >= code.max_uses:
        return _("Op"), "neutral"
    return _("Actief"), "success"


class DiscountList(StaffRequiredMixin, generic.ListView):
    permission_required = "billing.view_discountcode"
    template_name = "dashboard/discount_list.html"
    context_object_name = "codes"
    paginate_by = 25

    def get_queryset(self):
        return DiscountCode.objects.order_by("code")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        today = timezone.localdate()
        context["rows"] = [
            {"code": code, "status": discount_status(code, today)}
            for code in context["codes"]
        ]
        context["percent"] = DiscountKind.PERCENT
        return context


class DiscountFormMixin:
    model = DiscountCode
    form_class = DiscountCodeForm
    template_name = "dashboard/discount_form.html"
    success_url = reverse_lazy("dashboard:discount_list")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.object is not None:
            context["redemptions"] = self.object.redemptions.select_related(
                "order"
            ).order_by("-created_at", "-pk")[:20]
            context["status"] = discount_status(self.object, timezone.localdate())
        return context

    def saved(self, code, text):
        # The service logs the change.
        messages.success(self.request, text % {"code": code.code})
        return redirect(self.success_url)


class DiscountCreate(StaffRequiredMixin, DiscountFormMixin, generic.CreateView):
    permission_required = "billing.add_discountcode"

    def form_valid(self, form):
        try:
            self.object = create_discount_code(form.changes(), user=self.request.user)
        except ValidationError as error:
            form.add_error(None, error)
            return self.form_invalid(form)
        return self.saved(self.object, _("De kortingscode %(code)s is toegevoegd."))


class DiscountUpdate(StaffRequiredMixin, DiscountFormMixin, generic.UpdateView):
    """
    Change a discount code. The service locks the code and checks the rules
    against its current uses: a guest may use it meanwhile.
    """

    permission_required = "billing.change_discountcode"

    def form_valid(self, form):
        try:
            code = update_discount_code(
                self.object.pk, form.changes(), user=self.request.user
            )
        except DiscountCode.DoesNotExist:
            raise Http404
        except ValidationError as error:
            form.add_error(None, error)
            return self.form_invalid(form)
        self.object = code
        return self.saved(code, _("De wijzigingen in %(code)s zijn opgeslagen."))


# Invoices


def invoice_search_q(query):
    """Every word is in the number, the customer name or the e-mail address."""
    condition = Q()
    for word in query.split():
        condition &= (
            Q(number__icontains=word)
            | Q(customer_name__icontains=word)
            | Q(customer_email__icontains=word)
        )
    return condition


class InvoiceFilterMixin:
    """The invoices of the list and the CSV export, filtered with the GET parameters."""

    @cached_property
    def filter_form(self):
        form = InvoiceFilterForm(self.request.GET)
        # Invalid fields are left out of cleaned_data; the form shows their errors.
        form.is_valid()
        return form

    def get_queryset(self):
        invoices = Invoice.objects.select_related("credited_invoice")
        data = self.filter_form.cleaned_data
        if data.get("q"):
            invoices = invoices.filter(invoice_search_q(data["q"]))
        if data.get("soort"):
            invoices = invoices.filter(kind=data["soort"])
        if data.get("van"):
            invoices = invoices.filter(issue_date__gte=data["van"])
        if data.get("tot"):
            invoices = invoices.filter(issue_date__lte=data["tot"])
        return invoices.order_by("-issue_date", "-year", "-sequence")


def querystring_without_page(request):
    params = request.GET.copy()
    params.pop("page", None)
    return params.urlencode()


class InvoiceList(StaffRequiredMixin, InvoiceFilterMixin, generic.ListView):
    permission_required = "billing.view_invoice"
    template_name = "dashboard/invoice_list.html"
    context_object_name = "invoices"
    paginate_by = 25

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        querystring = querystring_without_page(self.request)
        context.update(
            {
                "form": self.filter_form,
                "querystring": querystring,
                "is_filtered": bool(querystring),
                "totals": self.get_queryset().aggregate(
                    excl=Sum("total_excl"),
                    vat=Sum("vat_amount"),
                    incl=Sum("total_incl"),
                ),
                "credit_note": InvoiceKind.CREDIT_NOTE,
            }
        )
        return context


class InvoiceExport(
    StaffRequiredMixin, InvoiceFilterMixin, CsvExportMixin, generic.View
):
    """The filtered invoices as CSV for Excel, for the bookkeeping."""

    permission_required = "billing.view_invoice"
    export_name = "invoice"
    filename_prefix = "facturen"
    header = (
        gettext_lazy("Nummer"),
        gettext_lazy("Datum"),
        gettext_lazy("Soort"),
        gettext_lazy("Bij factuur"),
        gettext_lazy("Boeking"),
        gettext_lazy("Klant"),
        gettext_lazy("E-mail"),
        gettext_lazy("Hotel"),
        gettext_lazy("Kamer"),
        gettext_lazy("Inchecken"),
        gettext_lazy("Uitchecken"),
        gettext_lazy("Nachten"),
        gettext_lazy("Btw-land"),
        gettext_lazy("Btw-tarief"),
        gettext_lazy("Excl. btw"),
        gettext_lazy("Btw"),
        gettext_lazy("Incl. btw"),
        gettext_lazy("Testfactuur"),
    )

    def get_row(self, invoice):
        return (
            invoice.number,
            invoice.issue_date.isoformat(),
            invoice.get_kind_display(),
            invoice.credited_invoice.number if invoice.credited_invoice else "",
            invoice.order_id,
            invoice.customer_name,
            invoice.customer_email,
            invoice.hotel_name,
            invoice.room_type,
            invoice.start_date.isoformat(),
            invoice.end_date.isoformat(),
            invoice.nights,
            invoice.vat_country,
            "" if invoice.vat_rate is None else csv_amount(invoice.vat_rate),
            csv_amount(invoice.total_excl),
            csv_amount(invoice.vat_amount),
            csv_amount(invoice.total_incl),
            _("ja") if invoice.is_test else _("nee"),
        )

    def filters_for_log(self):
        return filters_for_log(self.filter_form)

    def get_items(self):
        return self.get_queryset().iterator(chunk_size=500)


class InvoicePdf(StaffRequiredMixin, generic.View):
    """The PDF of an invoice or credit note, as stored when it was issued."""

    permission_required = "billing.view_invoice"

    def get(self, request, *args, **kwargs):
        invoice = get_object_or_404(
            Invoice.objects.select_related("order", "credited_invoice"), pk=kwargs["pk"]
        )
        content = invoice_pdf_bytes(invoice)
        logger.info("invoice pdf user=%s invoice=%s", request.user.pk, invoice.pk)
        response = HttpResponse(content, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{invoice.filename}"'
        return response


# Customers


class CustomerQueryMixin:
    @cached_property
    def filter_form(self):
        form = CustomerFilterForm(self.request.GET)
        form.is_valid()
        return form

    @cached_property
    def sort(self):
        return valid_sort(self.request.GET.get("sort", DEFAULT_SORT))

    def get_queryset(self):
        return customers(q=self.filter_form.cleaned_data.get("q") or "", sort=self.sort)


class CustomerList(StaffRequiredMixin, CustomerQueryMixin, generic.ListView):
    """
    The customers (e-mail addresses) with their bookings. Three queries,
    whatever the number of customers: the count, the page and the names.
    """

    permission_required = "base.view_customers"
    template_name = "dashboard/customer_list.html"
    context_object_name = "customer_page"
    paginate_by = 25

    columns = (
        ("email", gettext_lazy("Klant")),
        ("boekingen", gettext_lazy("Boekingen")),
        ("annuleringen", gettext_lazy("Annuleringen")),
        ("besteed", gettext_lazy("Totaal besteed")),
        ("laatste_verblijf", gettext_lazy("Laatste verblijf")),
    )

    def sort_links(self):
        """The column headers: clicking one sorts on it, again reverses it."""
        params = self.request.GET.copy()
        params.pop("page", None)
        links = []
        for key, label in self.columns:
            if self.sort == key:
                direction, next_sort = "ascending", f"-{key}"
            elif self.sort == f"-{key}":
                direction, next_sort = "descending", key
            else:
                # Numbers and dates start with the highest, the e-mail with A.
                direction, next_sort = "", key if key == "email" else f"-{key}"
            params["sort"] = next_sort
            links.append(
                {"label": label, "direction": direction, "query": params.urlencode()}
            )
        return links

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        querystring = querystring_without_page(self.request)
        context.update(
            {
                "customers": attach_names(context["customer_page"]),
                "form": self.filter_form,
                "sort_links": self.sort_links(),
                "sort": self.sort,
                "querystring": querystring,
                "is_filtered": bool(self.filter_form.cleaned_data.get("q")),
            }
        )
        return context


def chunks(iterable, size):
    iterator = iter(iterable)
    while chunk := list(islice(iterator, size)):
        yield chunk


class CustomerExport(
    StaffRequiredMixin, CustomerQueryMixin, CsvExportMixin, generic.View
):
    permission_required = "base.view_customers"
    export_name = "customer"
    filename_prefix = "klanten"
    header = (
        gettext_lazy("Naam"),
        gettext_lazy("E-mail"),
        gettext_lazy("Boekingen"),
        gettext_lazy("Annuleringen"),
        gettext_lazy("Totaal besteed"),
        gettext_lazy("Laatste verblijf"),
        gettext_lazy("Laatste boeking"),
    )

    def get_row(self, row):
        return (
            row["name"],
            row["key"],
            row["bookings"],
            row["cancellations"],
            csv_amount(row["spent"]),
            row["last_stay"].isoformat() if row["last_stay"] else "",
            row["last_order_id"],
        )

    def filters_for_log(self):
        filters = filters_for_log(self.filter_form)
        sort = f"sort={self.sort}"
        return sort if filters == "-" else f"{filters},{sort}"

    def get_items(self):
        # The names per chunk: one query per 500 customers.
        for chunk in chunks(self.get_queryset().iterator(chunk_size=500), 500):
            yield from attach_names(chunk)


class CustomerDetail(StaffRequiredMixin, generic.TemplateView):
    """
    A customer: their bookings and invoices. Reached through the number of one
    of their bookings, so no e-mail address goes into the URL.
    """

    permission_required = "base.view_customers"
    template_name = "dashboard/customer_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        order = get_object_or_404(Order.objects.only("pk"), pk=kwargs["order_pk"])
        orders = list(
            customer_orders(order)
            .select_related("hotel__city", "room")
            .order_by("-start_date", "-pk")
        )
        newest = max(orders, key=lambda item: item.pk)
        context.update(
            {
                "orders": orders,
                "name": f"{newest.first_name} {newest.last_name}".strip(),
                "email": newest.key,
                "summary": customer_summary(order),
                "invoices": Invoice.objects.filter(
                    order__in=[item.pk for item in orders]
                )
                .select_related("credited_invoice")
                .order_by("-issue_date", "-year", "-sequence"),
                "credit_note": InvoiceKind.CREDIT_NOTE,
            }
        )
        return context


# Reports


class Report(StaffRequiredMixin, generic.TemplateView):
    """Occupancy and revenue per hotel over a period (default: this month)."""

    permission_required = "base.view_reports"
    template_name = "dashboard/report.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        today = timezone.localdate()
        form = ReportFilterForm(self.request.GET)
        start, end = form.period(default=current_month(today))
        hotel = form.cleaned_data.get("hotel")
        rows = hotel_report(start, end, hotel_ids=[hotel.pk] if hotel else None)
        context.update(
            {
                "form": form,
                "start": start,
                "last": end - timedelta(days=1),
                "nights": (end - start).days,
                "rows": rows,
                "total": report_total(rows, name=_("Totaal")),
                "hotel": hotel,
                "month_links": self.month_links(start, hotel, today),
            }
        )
        return context

    def month_links(self, start, hotel, today):
        """Links to the month before and after the start, and to this month."""
        month = start.replace(day=1)
        previous = (month - timedelta(days=1)).replace(day=1)
        following = (month + timedelta(days=32)).replace(day=1)
        links = []
        for label, first in (
            (_("Vorige maand"), previous),
            (_("Deze maand"), today.replace(day=1)),
            (_("Volgende maand"), following),
        ):
            params = {"van": first.isoformat()}
            if hotel:
                params["hotel"] = hotel.pk
            links.append({"label": label, "query": urlencode(params)})
        return links
