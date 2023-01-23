from .models import *
from django.views import generic
import io
from django.core.management import call_command
import base64
from form.forms import FormOrder, FormCustomer
from django.urls import reverse_lazy
# Create your views here.

class Index(generic.ListView):
    model = City
    template_name = 'index.html'
    context_object_name = 'data'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['data'] = City.objects.values('name').distinct()
        return context

# Class-Based View voor de stad pagina ik geef de stad naam mee als parameter. Altijd met een hoofdletter de naam van een class
class Stad(generic.ListView):
    paginate_by = 4
    model = Hotel
    template_name = 'stad.html'

    def get_queryset(self):
        return Hotel.objects.filter(city__name=self.kwargs['city_name'])
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['city_name'] = self.kwargs['city_name']
        return context

    """"
    Ik filter hier op de stad naam die ik meegeef als paramter binnen de url en die gebruik ik dan voor de paginate. 
    Eerst filterde ik op alle steden binnen in de database, en toen kreeg ik alle hotels van alle steden in de pagina te zien wat niet de bedoeling is.
    """

class HotelList(generic.ListView):
    model = City
    template_name = 'hotels.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['city_name'] = City.objects.values('name').distinct()
        return context

class HotelDetail(generic.DetailView):
    model = Hotel
    template_name = 'hotel.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['rooms'] = Room.objects.filter(hotel=self.kwargs['pk'])
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

class OrderForm(generic.FormView):
    template_name = 'order.html'
    form_class = FormOrder
    success_url = reverse_lazy('order_customer')

    def form_valid(self, form):
        form.save()
        return super().form_valid(form)

class CustomerForm(generic.FormView):
    template_name = 'order_customer.html'
    form_class = FormCustomer
    success_url = reverse_lazy('index')

    def form_valid(self, form):
        form.save()
        return super().form_valid(form)
