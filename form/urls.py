from django.urls import path
from .views import *

urlpatterns = [
    path('contact/', ContactSave.as_view(), name='contact'),
]