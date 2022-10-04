from django.shortcuts import render
from .models import Data
# Create your views here.
def index(request):
    context = {
        'data': Data.objects.all()
    }
    return render(request, 'index.html', context)

def error_404(request, exception):
        data = {}
        return render(request,'404.html', data)

# Hier filter ik de data op de stad Amsterdam, en deze view is voor de pagina Amsterdam.
def amsterdam(request):
    context = {
        'data': Data.objects.filter(city_name='Amsterdam')
    }
    return render(request, 'amsterdam.html', context)
