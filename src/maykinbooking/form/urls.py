from django.urls import path

from .views import ContactSave

urlpatterns = [
    path("contact/", ContactSave.as_view(), name="contact"),
]
