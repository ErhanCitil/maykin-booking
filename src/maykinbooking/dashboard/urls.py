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
    path(
        "kamers/<int:pk>/blokkades/",
        views.RoomBlocks.as_view(),
        name="room_blocks",
    ),
    path(
        "blokkades/<int:pk>/verwijderen/",
        views.RoomBlockDelete.as_view(),
        name="room_block_delete",
    ),
    path("steden/nieuw/", views.CityCreate.as_view(), name="city_create"),
    path("boekingen/", views.BookingList.as_view(), name="booking_list"),
    path(
        "boekingen/export.csv",
        views.BookingExport.as_view(),
        name="booking_export",
    ),
    path(
        "boekingen/<int:pk>/",
        views.BookingDetail.as_view(),
        name="booking_detail",
    ),
    path(
        "boekingen/<int:pk>/annuleren/",
        views.BookingCancel.as_view(),
        name="booking_cancel",
    ),
    path(
        "boekingen/<int:pk>/mail-opnieuw/",
        views.BookingResend.as_view(),
        name="booking_resend",
    ),
]
