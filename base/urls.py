from django.urls import path
from .views import *

urlpatterns = [
    path('', Index.as_view(), name='index'),
    path('stad/<str:city_name>', Stad.as_view(), name='stad'),
    path('hotels/', HotelDetail.as_view(), name='hotel'),
    path('database_schema/', DatabaseSchema.as_view(), name='database_schema'),
]