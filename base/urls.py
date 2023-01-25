from django.urls import path
from .views import *
from django.conf import settings
from django.conf.urls.static import static
from django.contrib.staticfiles.urls import staticfiles_urlpatterns

urlpatterns = [
    path('', Index.as_view(), name='index'),
    path('stad/<str:city_name>', Stad.as_view(), name='stad'),
    path('database_schema/', DatabaseSchema.as_view(), name='database_schema'),
    path('hotels/', HotelList.as_view(), name='hotel'),
    path('hotel/<int:pk>', HotelDetail.as_view(), name='hotel_detail'),
    path('order/', OrderWizard.as_view([OrderForm1, OrderForm2]), name='order'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
