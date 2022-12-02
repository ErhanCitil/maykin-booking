from django.shortcuts import render
from .models import Data, ContactForm
from django.shortcuts import render
from django.views.generic.edit import FormView, CreateView, UpdateView
from .forms import FormContact

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
# def stad(request, city_name):
#     # Hier gebruik ik de .filter() functie om de data te filteren op basis van de stad naam die ik meegeef als parameter die ook in de url staat.
#     if Data.objects.filter(city_name=city_name).exists():
#         context = {
#             'data': Data.objects.filter(city_name=city_name)
#         }
#         return render(request, 'stad.html', context)
#     else:
#         return render(request, '404.html')

class stad(CreateView):
    model = Data
    fields = ['city_name']
    template_name = 'stad.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['data'] = Data.objects.filter(city_name=self.kwargs['city_name'])
        return context

def hotel(request):
    context = {
         'data': Data.objects.values('city_name').distinct()
    }
    return render(request, 'hotels.html',context)



# def contact(request):
#     if request.method == 'POST':
#         naamenachternaam = request.POST.get('naam')
#         email = request.POST.get('email')
#         onderwerp = request.POST.get('onderwerp')
#         bericht = request.POST.get('bericht')
#         contactform = ContactForm(naamenachternaam=naamenachternaam, email=email, onderwerp=onderwerp, bericht=bericht)
#         contactform.save()
#         return render(request, 'contact.html')

#     else:
#         return render(request, 'contact.html')