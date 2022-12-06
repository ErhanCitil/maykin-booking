from django.urls import path
from .views import *

urlpatterns = [
    path('', Index.as_view(), name='index'),
    path('stad/<str:city_name>', Stad.as_view(), name='stad'),
    path('hotels/', Hotel.as_view(), name='hotel'),
    path('contact/', ContactSave.as_view(), name='contact'),
]