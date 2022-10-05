from django.shortcuts import render
from .models import Data
from django.shortcuts import render, get_object_or_404
# Create your views here.
def index(request):
    context = {
        'data': Data.objects.values('city_name').distinct()
    }
    return render(request, 'index.html',context)

def error_404(request, exception):
        data = {}
        return render(request,'404.html', data)

def stad(request, city_name):
    if Data.objects.filter(city_name=city_name).exists():
        context = {
            'data': Data.objects.filter(city_name=city_name)
        }
        return render(request, 'stad.html', context)
    else:
        return render(request, '404.html')