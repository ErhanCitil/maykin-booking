"""
Simple rate limits on the default cache.

A fixed window per key: the first hit creates a counter that expires after
``seconds``, every hit after that increments it. Keys are hashed, so e-mail
addresses and IP addresses are not stored in the cache as plain text.

The limits are only as shared as the cache: with the local-memory cache every
process counts on its own. If the cache is unreachable (django-redis with
``IGNORE_EXCEPTIONS``), nothing is limited.
"""
import hashlib
import ipaddress
import logging

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

# IPv6 clients usually get a whole /64; counting per address would let one
# client use 2**64 addresses.
IPV6_PREFIX_LENGTH = 64


def client_ip(request):
    """
    The IP address of the client, or ``""`` when it is missing or invalid.

    Without trusted proxies (``BOOKING_TRUSTED_PROXY_COUNT = 0``) that is
    ``REMOTE_ADDR``. Behind N trusted proxies, which each add the address they
    got the request from to ``X-Forwarded-For``, it is the N-th address from the
    right. Everything to the left of that comes from the client and can be made
    up, so the leftmost address is never used. If the header holds fewer than N
    addresses, ``REMOTE_ADDR`` is used.

    An IPv4 address in IPv6 notation (``::ffff:192.0.2.1``) is returned as IPv4,
    and the zone of an IPv6 address (``fe80::1%eth0``) is left out.
    """
    address = request.META.get("REMOTE_ADDR", "")
    proxies = settings.BOOKING_TRUSTED_PROXY_COUNT
    if proxies > 0:
        header = request.META.get("HTTP_X_FORWARDED_FOR", "")
        forwarded = [part.strip() for part in header.split(",")] if header else []
        if len(forwarded) >= proxies:
            address = forwarded[-proxies]
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return ""
    if ip.version == 6:
        if ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
        return str(ipaddress.IPv6Address(int(ip)))  # without the zone
    return str(ip)


def ip_limit_key(ip):
    """The part of the throttle key for ``ip``: the address, or its /64 for IPv6."""
    address = ipaddress.ip_address(ip)
    if address.version == 6:
        network = ipaddress.ip_network(f"{address}/{IPV6_PREFIX_LENGTH}", strict=False)
        return str(network)
    return str(address)


def email_limit_key(email):
    """
    The part of the throttle key for an e-mail address: lowercase and without a
    ``+suffix`` in the local part (``Alex+1@Example.nl`` -> ``alex@example.nl``).
    Only for counting; the address of the booking stays as the guest typed it.
    """
    local, at, domain = email.strip().lower().rpartition("@")
    if not at:
        return domain
    return f"{local.split('+', 1)[0]}@{domain}"


def over_limit(key, limit, seconds):
    """
    Count one hit for ``key`` and return ``True`` if there have been more than
    ``limit`` hits in the current window of ``seconds``.
    """
    cache_key = "throttle:" + hashlib.sha256(key.encode()).hexdigest()
    if cache.add(cache_key, 1, timeout=seconds):
        count = 1
    else:
        try:
            count = cache.incr(cache_key)
        except ValueError:
            # The counter expired between add() and incr(): a new window.
            cache.add(cache_key, 1, timeout=seconds)
            count = 1
    return count is not None and count > limit


def ip_over_limit(request, scope, limit, seconds):
    """
    :func:`over_limit` per client (see :func:`client_ip`), with the key
    ``<scope>:ip:<address or IPv6 /64>``.

    Without a valid client IP address nothing is limited and a warning is
    logged: all those requests would otherwise share one counter.
    """
    ip = client_ip(request)
    if not ip:
        logger.warning("%s without client IP address: IP limit skipped", scope)
        return False
    return over_limit(f"{scope}:ip:{ip_limit_key(ip)}", limit, seconds)
