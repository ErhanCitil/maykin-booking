"""
Mails to the guest about a booking.

Every mail gets an ``EmailLog`` row (without the body). The templates are in
``templates/emails/``: ``<kind>_subject.txt``, ``<kind>.txt`` and ``<kind>.html``.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone, translation

from maykinbooking.base.models import EmailKind, EmailLog, EmailStatus, Order

from .pdf import render_order_pdf
from .services.booking import can_guest_cancel, guest_cancellation_deadline

logger = logging.getLogger(__name__)

# EmailLog.error keeps the start of the error; the full traceback is in the log.
MAX_ERROR_LENGTH = 500

TEMPLATES = {
    EmailKind.BOOKING_CONFIRMATION: "emails/booking_confirmation",
    EmailKind.BOOKING_CANCELLATION: "emails/booking_cancellation",
}


def my_booking_url(order):
    """
    Absolute URL of the "Mijn boeking" page. Built with ``SITE_URL``, never with
    the Host header of a request.
    """
    path = reverse("bookings:detail", kwargs={"token": order.token})
    return settings.SITE_URL.rstrip("/") + path


def last_free_cancellation_day(deadline):
    """
    The last date on which a guest can cancel before ``deadline``, in the time
    zone of the hotels (``TIME_ZONE``), whatever time zone is active.
    """
    local = timezone.localtime(deadline, timezone.get_default_timezone())
    return local.date() - timedelta(days=1)


def booking_context(order):
    """Template context for the mails and the PDF of an order."""
    deadline = guest_cancellation_deadline(order)
    return {
        "order": order,
        "nights": list(order.nights.all()),
        "night_count": order.night_count,
        "my_booking_url": my_booking_url(order),
        "cancel_deadline": deadline,
        # The deadline is 00:00 at the start of a day, so the last day on which a
        # guest can still cancel for free is the day before. Show this to guests
        # ("t/m vrijdag 4 december") instead of "tot zaterdag 00:00".
        "last_free_cancellation_day": last_free_cancellation_day(deadline),
        "can_cancel": can_guest_cancel(order),
        "contact_email": settings.BOOKING_CONTACT_EMAIL,
    }


def _build_message(order, kind):
    template = TEMPLATES[kind]
    with translation.override(order.language or settings.LANGUAGE_CODE):
        context = booking_context(order)
        # A subject is one line: drop the newlines and indentation of the template.
        subject = " ".join(render_to_string(f"{template}_subject.txt", context).split())
        text = render_to_string(f"{template}.txt", context)
        html = render_to_string(f"{template}.html", context)
        pdf = None
        if kind == EmailKind.BOOKING_CONFIRMATION:
            pdf = render_order_pdf(order)

    message = EmailMultiAlternatives(
        subject=subject,
        body=text,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[order.email],
        reply_to=[settings.BOOKING_CONTACT_EMAIL],
    )
    message.attach_alternative(html, "text/html")
    if pdf is not None:
        message.attach(f"boeking_{order.pk}.pdf", pdf, "application/pdf")
    return message


def send_booking_email(order_id, kind, *, triggered_by=None):
    """
    Send a mail of ``kind`` (an ``EmailKind``) about an order and log it.

    Never raises: it runs in ``transaction.on_commit`` after a booking, where an
    exception would turn a successful booking into an error page. A failure is
    logged (``logger.exception``) and stored in the ``EmailLog`` with status
    ``failed``. Returns the ``EmailLog``, or ``None`` if not even that could be
    created (unknown order or kind).

    ``triggered_by``: the staff member who (re)sent the mail, if any.
    """
    if kind not in TEMPLATES:
        logger.error("Unknown booking mail %r for order %s", kind, order_id)
        return None
    # The EmailLog writes each get their own (inner) transaction, so a database
    # error here cannot break a transaction of the caller.
    try:
        order = Order.objects.select_related("hotel__city", "room").get(pk=order_id)
        with transaction.atomic():
            log = EmailLog.objects.create(
                order=order,
                kind=kind,
                recipient=order.email,
                status=EmailStatus.PENDING,
                triggered_by=triggered_by,
            )
    except Exception:
        logger.exception("Could not start the %s mail of order %s", kind, order_id)
        return None

    try:
        message = _build_message(order, kind)
        log.subject = message.subject[:255]
        message.send()
    except Exception as exc:
        logger.exception("Sending the %s mail of order %s failed", kind, order_id)
        log.status = EmailStatus.FAILED
        log.error = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_LENGTH]
    else:
        log.status = EmailStatus.SENT
        log.sent_at = timezone.now()

    try:
        with transaction.atomic():
            log.save(update_fields=["subject", "status", "error", "sent_at"])
    except Exception:
        logger.exception("Could not update EmailLog %s", log.pk)
    return log
