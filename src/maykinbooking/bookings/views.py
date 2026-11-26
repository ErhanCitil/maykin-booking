"""
"Mijn boeking": the pages of a booking for the guest.

There is no login. The token in the URL (in the confirmation mail, and the
redirect after booking) is the only key, like a bearer secret: anyone with the
link can see and cancel the booking. So these pages are never cached, never
indexed and never send the URL on as referrer, and an unknown token is a plain
404.
"""
from django.conf import settings
from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect
from django.utils.decorators import method_decorator
from django.utils.translation import gettext
from django.views import generic
from django.views.decorators.cache import never_cache

from maykinbooking.base.models import Order

from .emails import booking_context
from .pdf import render_order_pdf
from .services.booking import (
    Actor,
    CancellationNotAllowed,
    InvalidTransition,
    cancel_booking,
)
from .throttle import ip_over_limit


@method_decorator(never_cache, name="dispatch")
class BookingTokenMixin(generic.detail.SingleObjectMixin):
    """
    The order of the token in the URL; never cached, never indexed, and links
    on the page do not leak the URL (with the token) to other sites.
    """

    queryset = Order.objects.select_related("hotel__city", "room")
    slug_field = "token"
    slug_url_kwarg = "token"
    context_object_name = "order"

    def dispatch(self, request, *args, **kwargs):
        response = super().dispatch(request, *args, **kwargs)
        response["X-Robots-Tag"] = "noindex"
        response["Referrer-Policy"] = "no-referrer"
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # The same data as the mails and the PDF.
        context.update(booking_context(self.object))
        return context


class BookingDetailView(BookingTokenMixin, generic.DetailView):
    template_name = "bookings/detail.html"


class BookingPDFView(BookingTokenMixin, generic.View):
    """The confirmation as PDF: the same file as the attachment of the mail."""

    def get(self, request, *args, **kwargs):
        # Rendering a PDF is expensive; this also slows down guessing tokens.
        if ip_over_limit(
            request, "booking-pdf", settings.BOOKING_PDF_LIMIT_PER_IP_PER_MINUTE, 60
        ):
            response = HttpResponse(
                gettext("Te veel downloads. Probeer het over een minuut opnieuw."),
                status=429,
                content_type="text/plain; charset=utf-8",
            )
            response["Retry-After"] = "60"
            return response
        order = self.get_object()
        response = HttpResponse(render_order_pdf(order), content_type="application/pdf")
        response[
            "Content-Disposition"
        ] = f'attachment; filename="boeking_{order.pk}.pdf"'
        return response


class BookingCancelView(BookingTokenMixin, generic.DetailView):
    """
    GET asks for confirmation, POST cancels. After the free cancellation
    deadline the guest gets an explanation and the contact address instead;
    cancelling twice changes nothing.
    """

    template_name = "bookings/cancel.html"

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        try:
            order, changed = cancel_booking(self.object.pk, actor=Actor.guest())
        except (CancellationNotAllowed, InvalidTransition):
            # Nothing changed: show why, with the contact address.
            context = self.get_context_data(object=self.object, refused=True)
            return self.render_to_response(context)

        if changed:
            messages.success(
                request,
                gettext(
                    "Je boeking is geannuleerd. Je krijgt hiervan een bevestiging "
                    "per e-mail."
                ),
            )
        else:
            messages.info(request, gettext("Deze boeking was al geannuleerd."))
        return redirect("bookings:detail", token=order.token)
