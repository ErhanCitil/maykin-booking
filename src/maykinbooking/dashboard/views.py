from functools import cached_property

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib.messages.views import SuccessMessageMixin
from django.db.models import Count, Min
from django.shortcuts import get_object_or_404
from django.urls import reverse, reverse_lazy
from django.views import generic

from maykinbooking.base.models import City, Hotel, Order, Room

from .forms import CityForm, HotelForm, RoomForm, next_hotel_id


class StaffRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    """
    Only staff users (admins and employees) can use the dashboard.
    Anonymous users are sent to the login page, other users get a 403.
    """

    login_url = reverse_lazy("login")

    def test_func(self):
        return self.request.user.is_staff


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
        context.update(
            {
                "stats": [
                    ("Hotels", Hotel.objects.count()),
                    ("Steden", City.objects.count()),
                    ("Kamers", Room.objects.count()),
                    ("Boekingen", Order.objects.exclude(first_name="").count()),
                ],
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
    """Confirmation page with the consequences of deleting, and a message after."""

    template_name = "dashboard/confirm_delete.html"

    def get_label(self, obj):
        return str(obj)

    def get_consequences(self):
        return []

    def get_cancel_url(self):
        raise NotImplementedError

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "label": self.get_label(self.object),
                "consequences": self.get_consequences(),
                "cancel_url": self.get_cancel_url(),
            }
        )
        return context

    def delete(self, request, *args, **kwargs):
        messages.success(request, f"{self.get_label(self.get_object())} is verwijderd.")
        return super().delete(request, *args, **kwargs)


class HotelDelete(StaffRequiredMixin, DeleteWithMessageMixin, generic.DeleteView):
    model = Hotel
    success_url = reverse_lazy("dashboard:home")

    def get_consequences(self):
        rooms = self.object.room.count()
        bookings = self.object.order.count()
        consequences = []
        if rooms:
            consequences.append(f"{rooms} kamer(s) worden ook verwijderd.")
        if bookings:
            consequences.append(f"{bookings} boeking(en) worden ook verwijderd.")
        return consequences

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

    def get_success_url(self):
        return reverse("dashboard:hotel_edit", args=[self.object.hotel.pk])


class RoomDelete(StaffRequiredMixin, DeleteWithMessageMixin, generic.DeleteView):
    model = Room

    def get_queryset(self):
        return Room.objects.select_related("hotel")

    def get_label(self, obj):
        return f"De kamer {obj.get_room_type_display()} van {obj.hotel.name}"

    def get_consequences(self):
        bookings = Order.objects.filter(room=self.object).count()
        if bookings:
            return [f"{bookings} boeking(en) voor deze kamer worden ook verwijderd."]
        return []

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
