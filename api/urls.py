from django.urls import path, include
from rest_framework import routers
from .views import *
from .serializers import *

router = routers.DefaultRouter()
router.register(r'city', CityViewSet)
router.register(r'hotel', HotelViewSet)
router.register(r'review', ReviewViewSet)

urlpatterns = [
    path('api/', include(router.urls)),
    path('api-auth/', include('rest_framework.urls', namespace='rest_framework'))
]
