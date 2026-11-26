from django import template

from maykinbooking.base.models import OrderStatus
from maykinbooking.bookings.services.booking import stay_is_over

register = template.Library()


@register.filter
def stay_over(order):
    """
    A confirmed booking whose stay has ended. It keeps the status "confirmed";
    the label "Verblijf voorbij" follows from the dates (see ``stay_is_over``).
    """
    return order.status == OrderStatus.CONFIRMED and stay_is_over(order)
