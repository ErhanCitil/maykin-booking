from django.urls import path

from . import views

app_name = "dashboard"

urlpatterns = [
    path("", views.DashboardHome.as_view(), name="home"),
    path("hotels/nieuw/", views.HotelCreate.as_view(), name="hotel_create"),
    path("hotels/<int:pk>/", views.HotelUpdate.as_view(), name="hotel_edit"),
    path(
        "hotels/<int:pk>/verwijderen/",
        views.HotelDelete.as_view(),
        name="hotel_delete",
    ),
    path(
        "hotels/<int:hotel_pk>/kamers/nieuw/",
        views.RoomCreate.as_view(),
        name="room_create",
    ),
    path("kamers/<int:pk>/", views.RoomUpdate.as_view(), name="room_edit"),
    path(
        "kamers/<int:pk>/verwijderen/",
        views.RoomDelete.as_view(),
        name="room_delete",
    ),
    path("steden/nieuw/", views.CityCreate.as_view(), name="city_create"),
]
