"""
Work after the commit that must never break the request that committed.
"""
import logging
from functools import partial

from django.db import transaction

logger = logging.getLogger(__name__)


def call_safely(func, *args, **kwargs):
    """
    Call ``func``; an exception is logged (with its traceback) and not raised.
    Returns the result, or ``None`` after an exception.
    """
    try:
        return func(*args, **kwargs)
    except Exception:
        logger.exception(
            "%s after the commit failed", getattr(func, "__qualname__", func)
        )
        return None


def on_commit_safely(func, *args, **kwargs):
    """
    ``transaction.on_commit`` for ``func(*args, **kwargs)`` that never raises:
    the booking or cancellation is committed already, so a failing mail, PDF or
    refund must not turn it into an error page. What failed is logged and is
    picked up again (the ``sync_payments`` cron retries refunds, a missing PDF
    is rendered when it is asked for, a mail can be sent again).
    """
    transaction.on_commit(partial(call_safely, func, *args, **kwargs))
