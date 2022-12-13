from .models import City, Hotel
from django.views import generic
# Create your views here.

class Index(generic.ListView):
    model = City
    template_name = 'index.html'
    context_object_name = 'data'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['data'] = City.objects.values('city_name').distinct()
        return context

# Class-Based View voor de stad pagina ik geef de stad naam mee als parameter. Altijd met een hoofdletter de naam van een class
class Stad(generic.ListView):
    model = Hotel
    template_name = 'stad.html'
    context_object_name = 'data'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['data'] = Hotel.objects.prefetch_related('city').filter(city__city_name=self.kwargs['city_name'])
        return context

# class Hotel(generic.ListView):
#     model = Data
#     template_name = 'hotels.html'
#     context_object_name = 'data'

#     def get_context_data(self, **kwargs):
#         context = super().get_context_data(**kwargs)
#         context['data'] = Data.objects.values('city_name').distinct()
#         return context