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

# Class-Based View voor de stad pagina ik geef de stad naam mee als parameter. Altijd met een hoofdletter de naam van een class
class Stad(CreateView):
    model = Data
    fields = ['city_name']
    template_name = 'stad.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['data'] = Data.objects.filter(city_name=self.kwargs['city_name'])
        return context

class ContactSave(FormView):
    template_name = 'contact.html'
    form_class = FormContact
    success_url = '/'

    def form_valid(self, form):
        form.save()
        return super().form_valid(form)

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