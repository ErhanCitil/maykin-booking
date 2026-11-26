from django.urls import path

from .views import FakeCheckoutView, PaymentReturnView, payment_webhook

app_name = "billing"

urlpatterns = [
    path(
        "betalen/<uuid:reference>/terug/",
        PaymentReturnView.as_view(),
        name="payment_return",
    ),
    path("betalen/webhook/", payment_webhook, name="payment_webhook"),
    # Development and CI only (404 unless PAYMENT_FAKE_PROVIDER).
    path(
        "nep-betaling/<str:payment_id>/",
        FakeCheckoutView.as_view(),
        name="fake_checkout",
    ),
]
