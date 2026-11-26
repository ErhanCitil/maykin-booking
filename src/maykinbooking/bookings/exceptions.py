"""
The base class of the errors of booking a room.

A module without imports, so the billing services (price rules, discount codes,
payments) can raise a ``BookingError`` without importing the booking service,
which imports them. ``bookings.services.booking`` exports it as well.
"""


class BookingError(Exception):
    """Base class of the errors of the booking service."""
