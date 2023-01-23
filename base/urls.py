from django.urls import path
from .views import *
from django.conf import settings
from django.conf.urls.static import static
from django.contrib.staticfiles.urls import staticfiles_urlpatterns

urlpatterns = [
    path('', Index.as_view(), name='index'),
    path('stad/<str:city_name>', Stad.as_view(), name='stad'),
<<<<<<< HEAD
    path('hotels/', HotelList.as_view(), name='hotel'),
    path('hotel/<int:pk>', HotelDetail.as_view(), name='hotel_detail'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
else:
    urlpatterns += staticfiles_urlpatterns()
=======
    path('hotels/', HotelDetail.as_view(), name='hotel'),
    path('order/', OrderForm.as_view(), name='order'),
    path('order/customer', CustomerForm.as_view(), name='order_customer'),
]
>>>>>>> 79abb6aadf8b81af8a8ea72d4ce6e81fe4c602ae
