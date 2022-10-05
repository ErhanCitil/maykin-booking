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
    return render(request, 'citys/amsterdam.html', context)

def antwerpen(request):
    context = {
        'data': Data.objects.filter(city_name='Antwerpen')
    }
    return render(request, 'citys/antwerpen.html', context)

def athene(request):
    context = {
        'data': Data.objects.filter(city_name='Athene')
    }
    return render(request, 'citys/athene.html', context)

def bangkok(request):
    context = {
        'data': Data.objects.filter(city_name='Bangkok')
    }
    return render(request, 'citys/bangkok.html', context)

def barcelona(request):
    context = {
        'data': Data.objects.filter(city_name='Barcelona')
    }
    return render(request, 'citys/barcelona.html', context)

def berlijn(request):
    context = {
        'data': Data.objects.filter(city_name='Berlijn')
    }
    return render(request, 'citys/berlijn.html', context)

