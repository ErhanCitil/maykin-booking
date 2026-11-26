from django.urls import path

from .views import BookingCancelView, BookingDetailView, BookingPDFView

app_name = "bookings"

urlpatterns = [
    path("boeking/<uuid:token>/", BookingDetailView.as_view(), name="detail"),
    path("boeking/<uuid:token>/pdf/", BookingPDFView.as_view(), name="pdf"),
    path(
        "boeking/<uuid:token>/annuleren/",
        BookingCancelView.as_view(),
        name="cancel",
    ),
]
