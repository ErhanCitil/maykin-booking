from .models import City, Hotel
from django.views import generic
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