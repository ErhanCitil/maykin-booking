from django.shortcuts import render
from .models import Data
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
    context = {
        'data': Data.objects.values('city_name').distinct()
    }
    return render(request, 'stad.html', context)