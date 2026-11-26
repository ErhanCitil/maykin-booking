"""
"Mijn boeking": the pages of a booking for the guest.

There is no login. The token in the URL (in the confirmation mail, and the
redirect after booking) is the only key, like a bearer secret: anyone with the
link can see and cancel the booking, and download its invoices. So these pages
are never cached, never indexed and never send the URL on as referrer, and an
unknown token is a plain 404.
"""
from django.conf import settings
from django.contrib import messages
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.defaultfilters import floatformat
from django.utils.decorators import method_decorator
from django.utils.translation import gettext
from django.views import generic
from django.views.decorators.cache import never_cache

from maykinbooking.base.models import Order, OrderStatus
from maykinbooking.billing.models import OPEN_PAYMENT_STATES, PaymentState
from maykinbooking.billing.services.invoicing import invoice_pdf_bytes
from maykinbooking.billing.services.payments import checkout_url_allowed
from maykinbooking.billing.services.pricing import night_lines

from .emails import booking_context
from .pdf import render_order_pdf
from .services.booking import (
    Actor,
    CancellationNotAllowed,
    InvalidTransition,
    cancel_booking,
)
from .throttle import ip_over_limit


def too_many_downloads():
    response = HttpResponse(
        gettext("Te veel downloads. Probeer het over een minuut opnieuw."),
        status=429,
        content_type="text/plain; charset=utf-8",
    )
    response["Retry-After"] = "60"
    return response


@method_decorator(never_cache, name="dispatch")
class BookingTokenMixin(generic.detail.SingleObjectMixin):
    """
    The order of the token in the URL; never cached, never indexed, and links
    on the page do not leak the URL (with the token) to other sites.
    """

    queryset = Order.objects.select_related("hotel__city", "room")
    context_object_name = "order"

    def get_object(self, queryset=None):
        """
        The order of the token, and only the token. (``SingleObjectMixin``
        would look up a ``pk`` in the URL first, such as the number of an
        invoice, and then ignore the token.)
        """
        if queryset is None:
            queryset = self.get_queryset()
        return get_object_or_404(queryset, token=self.kwargs["token"])

    def dispatch(self, request, *args, **kwargs):
        response = super().dispatch(request, *args, **kwargs)
        response["X-Robots-Tag"] = "noindex"
        response["Referrer-Policy"] = "no-referrer"
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # The same data as the mails and the PDF.
        context.update(booking_context(self.object))
        context["night_lines"] = night_lines(context["nights"])
        payment = context["payment"]
        context["paid_payment"] = (
            payment
            if payment is not None and payment.status == PaymentState.PAID
            else None
        )
        return context


class BookingDetailView(BookingTokenMixin, generic.DetailView):
    template_name = "bookings/detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # An order that waits for payment: back to the payment page (only an
        # allowed URL, see the payment service).
        payment = context["payment"]
        context["continue_payment_url"] = (
            payment.checkout_url
            if self.object.status == OrderStatus.PENDING_PAYMENT
            and payment is not None
            and payment.status in OPEN_PAYMENT_STATES
            and checkout_url_allowed(payment.checkout_url)
            else None
        )
        return context


class BookingPDFView(BookingTokenMixin, generic.View):
    """The confirmation as PDF: the same file as the attachment of the mail."""

    def get(self, request, *args, **kwargs):
        # Rendering a PDF is expensive; this also slows down guessing tokens.
        if ip_over_limit(
            request, "booking-pdf", settings.BOOKING_PDF_LIMIT_PER_IP_PER_MINUTE, 60
        ):
            return too_many_downloads()
        order = self.get_object()
        response = HttpResponse(render_order_pdf(order), content_type="application/pdf")
        response[
            "Content-Disposition"
        ] = f'attachment; filename="boeking_{order.pk}.pdf"'
        return response


class BookingInvoiceView(BookingTokenMixin, generic.View):
    """
    An invoice or credit note of the booking as PDF: the stored file, the same
    as the attachment of the mail. Only the invoices of the order of the token;
    any other number is a 404, whether it exists or not.
    """

    def get(self, request, *args, **kwargs):
        # The same limit as the confirmation PDF, counted on its own.
        if ip_over_limit(
            request,
            "booking-invoice",
            settings.BOOKING_PDF_LIMIT_PER_IP_PER_MINUTE,
            60,
        ):
            return too_many_downloads()
        order = self.get_object()
        invoice = order.invoices.filter(pk=kwargs["pk"]).first()
        if invoice is None:
            raise Http404
        response = HttpResponse(
            invoice_pdf_bytes(invoice), content_type="application/pdf"
        )
        response["Content-Disposition"] = f'attachment; filename="{invoice.filename}"'
        return response


class BookingCancelView(BookingTokenMixin, generic.DetailView):
    """
    GET asks for confirmation, POST cancels. After the free cancellation
    deadline the guest gets an explanation and the contact address instead;
    cancelling twice changes nothing. A paid booking is refunded (see
    ``cancel_booking``).
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
            paid = order.payments.filter(status=PaymentState.PAID).first()
            if paid is not None:
                message = gettext(
                    "Je boeking is geannuleerd. Je krijgt € %(amount)s teruggestort "
                    "en een bevestiging per e-mail."
                ) % {"amount": floatformat(paid.amount, 2)}
            else:
                message = gettext(
                    "Je boeking is geannuleerd. Je krijgt hiervan een bevestiging "
                    "per e-mail."
                )
            messages.success(request, message)
        else:
            messages.info(request, gettext("Deze boeking was al geannuleerd."))
        return redirect("bookings:detail", token=order.token)
