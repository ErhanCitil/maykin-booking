from django.contrib.auth.views import LogoutView
from django.urls import path
from django.views.generic import RedirectView
from .views import *
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('', Index.as_view(), name='index'),
    path('stad/<str:city_name>', Stad.as_view(), name='stad'),
    path('database_schema/', DatabaseSchema.as_view(), name='database_schema'),
    path('hotels/', HotelList.as_view(), name='hotel'),
    path('hotel/<int:pk>', HotelDetail.as_view(), name='hotel_detail'),
    path('order/<int:pk>', Checkout.as_view(), name='order'),
    path('success/<int:pk>', Success.as_view(), name='success'),
    path('orderpdf/<int:pk>', OrderPDF.as_view(), name='orderpdf'),
    # Old edit pages, replaced by the staff dashboard.
    path('hotel_edit/<int:pk>', RedirectView.as_view(pattern_name='dashboard:hotel_edit'), name='hotel_edit'),
    path('login/', Login.as_view(), name='login'),
    path('logout/', LogoutView.as_view(next_page='index'), name='logout'),
    path('hotels_edit/', RedirectView.as_view(pattern_name='dashboard:home'), name='hotels_edit'),
    path('terms/<int:pk>', Terms.as_view(), name='terms'),
]+ static(settings.MEDIA_URL, document_root = settings.MEDIA_ROOT)
