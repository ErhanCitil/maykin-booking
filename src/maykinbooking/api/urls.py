from django.urls import path, include
from rest_framework import routers
from maykinbooking.api import views
from .serializers import *

router = routers.DefaultRouter()
router.register(r'city', views.CityViewSet)
router.register(r'hotel', views.HotelViewSet)
router.register(r'review', views.ReviewViewSet)

urlpatterns = [
    path('api/', include(router.urls)),
    path('api-auth/', include('rest_framework.urls', namespace='rest_framework')),
    path('api-hotel/', views.HotelList.as_view()),
]
