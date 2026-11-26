"""
The public pages of online payment: where the provider sends the guest back,
the webhook of Mollie, and the checkout page of the fake provider.

None of them trusts what the request says about a payment: the status is always
asked of the provider (``sync_payment``).
"""
import logging
import re
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.translation import gettext
from django.views import generic
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from maykinbooking.base.models import OrderStatus
from maykinbooking.bookings.throttle import ip_over_limit

from .models import OPEN_PAYMENT_STATES, FakeProviderPayment, Payment, PaymentState
from .providers import ProviderError, ProviderNotAvailable
from .services.payments import NOT_CONFIRMING_REASONS, sync_payment

logger = logging.getLogger(__name__)

# Per IP address per minute.
RETURN_LIMIT_PER_MINUTE = 30
WEBHOOK_LIMIT_PER_MINUTE = 120
# The id Mollie (or the fake provider) posts to the webhook.
WEBHOOK_PAYMENT_ID = re.compile(r"(?:fake_)?tr_[A-Za-z0-9]{1,60}")
# The "wordt verwerkt" page reloads itself after this many seconds.
REFRESH_SECONDS = 5

FAKE_ACTIONS = {
    "paid": PaymentState.PAID,
    "failed": PaymentState.FAILED,
    "canceled": PaymentState.CANCELED,
}


def checkout_again_url(order):
    """The checkout of the hotel of ``order``, with its dates filled in."""
    query = urlencode(
        {"van": order.start_date.isoformat(), "tot": order.end_date.isoformat()}
    )
    return f"{reverse('order', args=[order.hotel_id])}?{query}"


def too_many_requests():
    response = HttpResponse(
        gettext("Te veel verzoeken. Probeer het over een minuut opnieuw."),
        status=429,
        content_type="text/plain; charset=utf-8",
    )
    response["Retry-After"] = "60"
    return response


def private(response):
    """Never indexed, and the URL (with the reference) never sent on."""
    response["X-Robots-Tag"] = "noindex"
    response["Referrer-Policy"] = "no-referrer"
    return response


@method_decorator(never_cache, name="dispatch")
class PaymentReturnView(generic.View):
    """
    The provider sends the guest back here. We ask the provider for the status
    (locally there is no webhook) and go on:

    * paid: to "Mijn boeking";
    * paid, but too late (the room was taken), twice, or another amount: a page
      that says the money is refunded;
    * still open, or the provider could not be reached: a "wordt verwerkt" page
      that reloads itself;
    * failed, canceled, expired: back to the checkout of the hotel with the
      dates of the booking and a message, to try again; or, when the booking
      was paid by another payment meanwhile, to "Mijn boeking".
    """

    template_name = "billing/payment_return.html"

    def get(self, request, reference):
        if ip_over_limit(request, "payment-return", RETURN_LIMIT_PER_MINUTE, 60):
            return private(too_many_requests())
        payment = get_object_or_404(
            Payment.objects.select_related("order"), reference=reference
        )
        try:
            payment = sync_payment(payment.pk)
        except ProviderError as exc:
            # The next reload tries again; the cron catches up as well.
            logger.warning("Return of payment %s: provider error: %s", payment.pk, exc)
        order = payment.order
        order.refresh_from_db()

        refund = (
            payment.refunds.filter(reason__in=NOT_CONFIRMING_REASONS)
            .order_by("-created_at", "-pk")
            .first()
        )
        if refund is not None:
            state = "refunded"
        elif payment.status == PaymentState.PAID:
            messages.success(request, gettext("Je betaling is ontvangen."))
            return private(redirect("bookings:detail", token=order.token))
        elif payment.status in OPEN_PAYMENT_STATES:
            state = "processing"
        else:
            return private(self.not_paid(request, order))
        response = render(
            request,
            self.template_name,
            {
                "state": state,
                "order": order,
                "payment": payment,
                "refund": refund,
                "checkout_url": checkout_again_url(order),
                "refresh_seconds": REFRESH_SECONDS,
                "contact_email": settings.BOOKING_CONTACT_EMAIL,
            },
        )
        return private(response)

    def not_paid(self, request, order):
        """The payment failed (or was canceled, or expired)."""
        if order.status in (OrderStatus.CONFIRMED, OrderStatus.CANCELLED):
            # Another payment of this booking went through.
            messages.info(
                request,
                gettext(
                    "Deze betaling is niet doorgegaan, maar je boeking was al "
                    "betaald. Er is niets afgeschreven voor deze betaling."
                ),
            )
            return redirect("bookings:detail", token=order.token)
        if order.status == OrderStatus.PENDING_PAYMENT:
            # Another payment of this booking is still open.
            messages.info(
                request,
                gettext(
                    "Deze betaling is niet doorgegaan. Je boeking wacht nog op "
                    "een betaling."
                ),
            )
            return redirect("bookings:detail", token=order.token)
        messages.error(
            request,
            gettext(
                "De betaling is niet gelukt, dus je boeking is niet gemaakt. "
                "Je kunt het opnieuw proberen."
            ),
        )
        return redirect(checkout_again_url(order))


@csrf_exempt
@require_POST
@never_cache
def payment_webhook(request):
    """
    Mollie posts ``id=tr_...`` when a payment changes. The status is fetched
    from Mollie, never taken from the request. An unknown id gets 200 without
    effect (Mollie must not keep retrying, and nobody learns which ids exist);
    503 when Mollie could not be reached, so it retries later.
    """
    if ip_over_limit(request, "payment-webhook", WEBHOOK_LIMIT_PER_MINUTE, 60):
        return too_many_requests()
    provider_id = request.POST.get("id", "")
    if not WEBHOOK_PAYMENT_ID.fullmatch(provider_id):
        return HttpResponseBadRequest()
    payment = Payment.objects.filter(provider_id=provider_id).first()
    if payment is None:
        logger.info("Webhook for an unknown payment")
        return HttpResponse(status=200)
    try:
        sync_payment(payment.pk)
    except ProviderNotAvailable as exc:
        # Retrying does not help; the cron reports it as well.
        logger.error("Webhook for payment %s: %s", payment.pk, exc)
        return HttpResponse(status=200)
    except ProviderError as exc:
        logger.warning("Webhook for payment %s: provider error: %s", payment.pk, exc)
        return HttpResponse(status=503)
    return HttpResponse(status=200)


@method_decorator(never_cache, name="dispatch")
class FakeCheckoutView(generic.View):
    """
    The payment page of the fake provider (development and CI only): pay, fail
    or cancel, then back to the return URL, like at Mollie. 404 unless
    ``PAYMENT_FAKE_PROVIDER`` is on.
    """

    template_name = "billing/fake_checkout.html"

    def dispatch(self, request, *args, **kwargs):
        if not settings.PAYMENT_FAKE_PROVIDER:
            raise Http404
        return private(super().dispatch(request, *args, **kwargs))

    def get_payment(self):
        return get_object_or_404(FakeProviderPayment, pk=self.kwargs["payment_id"])

    def get(self, request, payment_id):
        return render(request, self.template_name, {"payment": self.get_payment()})

    def post(self, request, payment_id):
        fake = self.get_payment()
        status = FAKE_ACTIONS.get(request.POST.get("action", ""))
        # Only ever back to this site.
        if status is None or not fake.redirect_url.startswith(
            settings.SITE_URL.rstrip("/") + "/"
        ):
            return HttpResponseBadRequest()
        paid = status == PaymentState.PAID
        FakeProviderPayment.objects.filter(pk=fake.pk, status=PaymentState.OPEN).update(
            status=status,
            method="ideal" if paid else "",
            paid_at=timezone.now() if paid else None,
        )
        return redirect(fake.redirect_url)
