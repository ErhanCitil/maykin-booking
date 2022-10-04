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