from django.shortcuts import render
from .models import Data
from django.shortcuts import render, get_object_or_404
# Create your views here.
def index(request):
    context = {
        # Met de .distinct() functie zorg ik ervoor dat ik alleen de unieke waardes van de kolom city_name krijg.
        'data': Data.objects.values('city_name').distinct()
    }
    return render(request, 'index.html',context)

# View voor de 404 pagina 
def error_404(request, exception):
        data = {}
        return render(request,'404.html', data)

# View voor de stad pagina ik geef de stad naam mee als parameter.
def stad(request, city_name):
    # Hier gebruik ik de .filter() functie om de data te filteren op basis van de stad naam die ik meegeef als parameter die ook in de url staat.
    if Data.objects.filter(city_name=city_name).exists():
        context = {
            'data': Data.objects.filter(city_name=city_name)
        }
        return render(request, 'stad.html', context)
    else:
        return render(request, '404.html')

def contact(request):
        return render(request, 'contact.html')

def hotel(request):
    context = {
        'data': Data.objects.values('city_name').distinct()
    }
    return render(request, 'hotels.html',context)
