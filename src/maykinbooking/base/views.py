from .models import City, Hotel, Room, Order, ROOM_CHOICES, Highlight
from django.db.models import Count, Min
from django.views import generic
import io
from django.core.management import call_command
import base64

from maykinbooking.form.forms import ConfirmForm, GuestForm, StayForm

from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from functools import cached_property
from django_countries import countries
from formtools.wizard.views import SessionWizardView

from django_weasyprint import WeasyTemplateResponseMixin

from django.http import Http404
from django.contrib.auth.views import LoginView
# Create your views here.

class Index(generic.ListView):
    model = City
    template_name = 'index.html'
    context_object_name = 'data'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['data'] = City.objects.values('name').annotate(hotel_count=Count('test')).order_by('name')
        return context

# Class-Based View voor de stad pagina ik geef de stad naam mee als parameter. Altijd met een hoofdletter de naam van een class
class Stad(generic.ListView):
    paginate_by = 12
    model = Hotel
    template_name = 'stad.html'

    def get_queryset(self):
        search = self.request.GET.get('q')
        if search:
            return Hotel.objects.filter(city__name=self.kwargs['city_name'], name__icontains=search).annotate(min_price=Min('room__price')).order_by('name')
        else:
            return Hotel.objects.filter(city__name=self.kwargs['city_name']).annotate(min_price=Min('room__price')).order_by('name')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['city_name'] = self.kwargs['city_name']
        return context

    """"
    Ik filter hier op de stad naam die ik meegeef als parameter binnen de url en die gebruik ik dan voor de paginate.
    Eerst filterde ik op alle steden binnen in de database, en toen kreeg ik alle hotels van alle steden in de pagina te zien wat niet de bedoeling is.
    """

class HotelList(generic.ListView):
    model = City
    template_name = 'hotels.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['city_name'] = City.objects.values('name').annotate(hotel_count=Count('test')).order_by('name')
        return context

class HotelDetail(generic.DetailView):
    model = Hotel
    template_name = 'hotel.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['rooms'] = Room.objects.filter(hotel=self.kwargs['pk'])
        context['min_price'] = context['rooms'].aggregate(p=Min('price'))['p'] or self.object.price
        context['highlights'] = Highlight.objects.filter(hotel=self.kwargs['pk'])
        return context

class DatabaseSchema(generic.TemplateView):
    template_name = 'database_schema.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        with io.StringIO() as out:
            call_command('graph_models', '-a', '-o', 'static/schema.png', stdout=out)
            with open('static/schema.png', 'rb') as image_file:
                encoded_string = base64.b64encode(image_file.read())
                context['schema'] = encoded_string
        return context

CHECKOUT_STEPS = [
    ('stay', 'Je verblijf'),
    ('guest', 'Je gegevens'),
    ('confirm', 'Controleren'),
]


class Checkout(SessionWizardView):
    """
    Three step checkout: choose your stay, enter your details, review and confirm.
    The order is only created when the guest confirms in the last step.
    """

    template_name = 'order.html'
    form_list = [
        ('stay', StayForm),
        ('guest', GuestForm),
        ('confirm', ConfirmForm),
    ]

    @cached_property
    def hotel(self):
        return get_object_or_404(Hotel.objects.select_related('city'), pk=self.kwargs['pk'])

    @property
    def bookable(self):
        return self.hotel.is_available and self.hotel.room.filter(is_available=True).exists()

    def get_form_kwargs(self, step=None):
        kwargs = super().get_form_kwargs(step)
        if step == 'stay':
            kwargs['hotel'] = self.hotel
        return kwargs

    def post(self, *args, **kwargs):
        if not self.bookable:
            return self.render(self.get_form())
        return super().post(*args, **kwargs)

    def get_stay(self):
        """The chosen stay with its price breakdown, once step 1 is completed."""
        if self.steps.current == 'stay':
            return None
        data = self.get_cleaned_data_for_step('stay')
        if not data:
            return None
        nights = (data['end_date'] - data['start_date']).days
        return {
            'start_date': data['start_date'],
            'end_date': data['end_date'],
            'nights': nights,
            'room': data['room'],
            'price_per_night': data['room'].price,
            'total': data['room'].price * nights,
        }

    def get_context_data(self, form, **kwargs):
        context = super().get_context_data(form=form, **kwargs)
        current_index = self.steps.index
        context.update({
            'hotel': self.hotel,
            'bookable': self.bookable,
            'stay': self.get_stay(),
            'checkout_steps': [
                {
                    'number': index + 1,
                    'label': label,
                    'done': index < current_index,
                    'current': index == current_index,
                }
                for index, (name, label) in enumerate(CHECKOUT_STEPS)
            ],
        })
        if self.steps.current == 'confirm':
            guest = self.get_cleaned_data_for_step('guest') or {}
            context['guest'] = {**guest, 'country_name': countries.name(guest.get('country', ''))}
        return context

    def done(self, form_list, form_dict, **kwargs):
        stay = form_dict['stay'].cleaned_data
        order = form_dict['guest'].save(commit=False)
        order.hotel = self.hotel
        order.room = stay['room']
        order.start_date = stay['start_date']
        order.end_date = stay['end_date']
        order.save()
        self.request.session['order_token'] = str(order.token)
        return redirect('success', pk=order.pk)


class GuestOrderMixin:
    """Only the guest who made the booking (same session) can see it."""

    model = Order
    context_object_name = 'order'

    def get_queryset(self):
        return Order.objects.select_related('hotel__city', 'room')

    def get_object(self, queryset=None):
        order = super().get_object(queryset)
        if self.request.session.get('order_token') != str(order.token):
            raise Http404
        return order


class Success(GuestOrderMixin, generic.DetailView):
    template_name = 'success.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['nights'] = (self.object.end_date - self.object.start_date).days
        return context


class OrderPDF(GuestOrderMixin, WeasyTemplateResponseMixin, generic.DetailView):
    template_name = 'order_pdf.html'

    def get_pdf_filename(self):
        return 'boeking_{}.pdf'.format(self.object.pk)

class Login(LoginView):
    template_name = 'login.html'
    fields = '__all__'
    redirect_authenticated_user = True

    def get_success_url(self):
        # Staff members go straight to the dashboard, unless ?next= says otherwise.
        if self.get_redirect_url():
            return self.get_redirect_url()
        if self.request.user.is_staff:
            return reverse('dashboard:home')
        return reverse('index')

class Terms(generic.TemplateView):
    template_name = 'terms.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['terms'] = Hotel.objects.get(id=self.kwargs['pk'])
        return context
