from django.urls import path
from .views import *
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('', Index.as_view(), name='index'),
    path('stad/<str:city_name>', Stad.as_view(), name='stad'),
    path('database_schema/', DatabaseSchema.as_view(), name='database_schema'),
    path('hotels/', HotelList.as_view(), name='hotel'),
    path('hotel/<int:pk>', HotelDetail.as_view(), name='hotel_detail'),
    path('order/<int:pk>', OrderWizard.as_view(), name='order'),
    path('success/<int:pk>', Success.as_view(), name='success'),
    path('hotel_edit/<int:pk>', HotelEdit.as_view(), name='hotel_edit'),
    path('login/', Login.as_view(), name='login'),
]+ static(settings.MEDIA_URL, document_root = settings.MEDIA_ROOT)
