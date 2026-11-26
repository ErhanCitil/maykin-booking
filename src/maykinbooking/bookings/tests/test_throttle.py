from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock

from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, override_settings

from freezegun import freeze_time

from ..throttle import (
    client_ip,
    email_limit_key,
    ip_limit_key,
    ip_over_limit,
    over_limit,
)

START = datetime(2027, 3, 1, 9, 0, tzinfo=dt_timezone.utc)


class OverLimitTests(SimpleTestCase):
    def setUp(self):
        # The counters live in the cache, which all tests share.
        cache.clear()
        self.addCleanup(cache.clear)

    def test_counts_hits_per_key(self):
        with freeze_time(START):
            self.assertEqual(
                [over_limit("a", 3, 60) for _ in range(5)],
                [False, False, False, True, True],
            )
            self.assertFalse(over_limit("b", 3, 60))

    def test_new_window_after_the_period(self):
        with freeze_time(START) as frozen:
            for _ in range(4):
                over_limit("a", 3, 60)
            self.assertTrue(over_limit("a", 3, 60))
            frozen.tick(timedelta(seconds=61))
            self.assertFalse(over_limit("a", 3, 60))

    def test_keys_are_hashed(self):
        with mock.patch("maykinbooking.bookings.throttle.cache") as fake_cache:
            fake_cache.add.return_value = True
            over_limit("booking:email:alex@example.com", 3, 60)
        (key, value), kwargs = fake_cache.add.call_args
        self.assertNotIn("alex", key)
        self.assertRegex(key, r"^throttle:[0-9a-f]{64}$")
        self.assertEqual((value, kwargs), (1, {"timeout": 60}))

    def test_unreachable_cache_limits_nothing(self):
        # django-redis with IGNORE_EXCEPTIONS returns None instead of raising.
        with mock.patch("maykinbooking.bookings.throttle.cache") as fake_cache:
            fake_cache.add.return_value = None
            fake_cache.incr.return_value = None
            self.assertFalse(over_limit("a", 0, 60))

    def test_counter_that_expired_between_add_and_incr(self):
        with mock.patch("maykinbooking.bookings.throttle.cache") as fake_cache:
            fake_cache.add.side_effect = [False, True]
            fake_cache.incr.side_effect = ValueError("Key not found")
            self.assertFalse(over_limit("a", 1, 60))
        self.assertEqual(fake_cache.add.call_count, 2)

    def test_ip_over_limit_counts_per_address(self):
        factory = RequestFactory()
        first = factory.get("/", REMOTE_ADDR="192.0.2.1")
        second = factory.get("/", REMOTE_ADDR="192.0.2.2")
        self.assertEqual(
            [ip_over_limit(first, "test", 1, 60) for _ in range(2)], [False, True]
        )
        self.assertFalse(ip_over_limit(second, "test", 1, 60))
        # The same key as over_limit with the address in it.
        self.assertTrue(over_limit("test:ip:192.0.2.1", 1, 60))

    def test_ip_over_limit_without_address_limits_nothing_and_warns(self):
        request = RequestFactory().get("/", REMOTE_ADDR="")
        with self.assertLogs("maykinbooking.bookings.throttle", "WARNING") as logs:
            self.assertFalse(
                any(ip_over_limit(request, "test", 0, 60) for _ in range(3))
            )
        self.assertEqual(
            logs.output[0],
            "WARNING:maykinbooking.bookings.throttle:"
            "test without client IP address: IP limit skipped",
        )

    def test_client_ip_is_remote_addr(self):
        request = RequestFactory().get(
            "/", REMOTE_ADDR="192.0.2.1", HTTP_X_FORWARDED_FOR="198.51.100.7"
        )
        self.assertEqual(client_ip(request), "192.0.2.1")


def request_from(remote_addr, forwarded_for=None):
    extra = {"REMOTE_ADDR": remote_addr}
    if forwarded_for is not None:
        extra["HTTP_X_FORWARDED_FOR"] = forwarded_for
    return RequestFactory().get("/", **extra)


class ClientIpTests(SimpleTestCase):
    """Security N1: the client address behind 0, 1 or 2 trusted proxies."""

    @override_settings(BOOKING_TRUSTED_PROXY_COUNT=0)
    def test_without_proxies_x_forwarded_for_is_ignored(self):
        request = request_from("192.0.2.1", "198.51.100.7")
        self.assertEqual(client_ip(request), "192.0.2.1")

    @override_settings(BOOKING_TRUSTED_PROXY_COUNT=1)
    def test_one_proxy_takes_the_rightmost_address(self):
        # nginx appends the address it got the request from.
        request = request_from("10.0.0.2", "203.0.113.9")
        self.assertEqual(client_ip(request), "203.0.113.9")

    @override_settings(BOOKING_TRUSTED_PROXY_COUNT=2)
    def test_two_proxies_take_the_second_address_from_the_right(self):
        # load balancer -> nginx: "<client>, <load balancer>"
        request = request_from("10.0.0.2", "203.0.113.9, 10.0.0.1")
        self.assertEqual(client_ip(request), "203.0.113.9")

    def test_a_spoofed_leftmost_address_is_never_used(self):
        for proxies, header, expected in [
            (1, "1.1.1.1, 203.0.113.9", "203.0.113.9"),
            (2, "1.1.1.1, 2.2.2.2, 203.0.113.9, 10.0.0.1", "203.0.113.9"),
        ]:
            with self.subTest(proxies=proxies), self.settings(
                BOOKING_TRUSTED_PROXY_COUNT=proxies
            ):
                self.assertEqual(client_ip(request_from("10.0.0.2", header)), expected)

    @override_settings(BOOKING_TRUSTED_PROXY_COUNT=2)
    def test_shorter_header_falls_back_to_remote_addr(self):
        for header in [None, "", "203.0.113.9"]:
            with self.subTest(header=header):
                request = request_from("10.0.0.2", header)
                self.assertEqual(client_ip(request), "10.0.0.2")

    @override_settings(BOOKING_TRUSTED_PROXY_COUNT=1)
    def test_invalid_address_is_empty(self):
        for header in ["unknown", "203.0.113.9:4711", "1.1.1.1, "]:
            with self.subTest(header=header):
                self.assertEqual(client_ip(request_from("10.0.0.2", header)), "")
        self.assertEqual(client_ip(request_from("not-an-ip")), "")

    @override_settings(BOOKING_TRUSTED_PROXY_COUNT=1)
    def test_invalid_address_skips_the_ip_limit(self):
        request = request_from("10.0.0.2", "1.1.1.1, garbage")
        with self.assertLogs("maykinbooking.bookings.throttle", "WARNING"):
            self.assertFalse(
                any(ip_over_limit(request, "test", 0, 60) for _ in range(3))
            )

    def test_ipv4_in_ipv6_notation_is_ipv4(self):
        self.assertEqual(client_ip(request_from("::ffff:192.0.2.1")), "192.0.2.1")

    def test_ipv6_is_normalised(self):
        self.assertEqual(client_ip(request_from("2001:DB8:0:0:0:0:0:1")), "2001:db8::1")
        # The zone of a link-local address is left out, also in the key.
        self.assertEqual(client_ip(request_from("fe80::1%eth0")), "fe80::1")
        self.assertEqual(
            ip_limit_key(client_ip(request_from("fe80::1%eth0"))), "fe80::/64"
        )


class IpLimitKeyTests(SimpleTestCase):
    """Security N2: IPv6 is counted per /64, IPv4 per address."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_keys(self):
        self.assertEqual(ip_limit_key("192.0.2.1"), "192.0.2.1")
        self.assertEqual(ip_limit_key("2001:db8:1:2:aaaa::1"), "2001:db8:1:2::/64")

    def test_addresses_in_the_same_ipv6_64_share_a_limit(self):
        same_64 = ["2001:db8:1:2::1", "2001:db8:1:2:ffff:ffff:ffff:ffff"]
        other_64 = "2001:db8:1:3::1"
        hits = [ip_over_limit(request_from(ip), "test", 1, 60) for ip in same_64]
        self.assertEqual(hits, [False, True])
        self.assertFalse(ip_over_limit(request_from(other_64), "test", 1, 60))

    def test_ipv4_addresses_have_their_own_limit(self):
        hits = [
            ip_over_limit(request_from(ip), "test", 1, 60)
            for ip in ["192.0.2.1", "192.0.2.2", "192.0.2.1"]
        ]
        self.assertEqual(hits, [False, False, True])


class EmailLimitKeyTests(SimpleTestCase):
    """Security N3: one limit for an address and its +suffix variants."""

    def test_keys(self):
        for email, key in [
            ("alex@example.com", "alex@example.com"),
            ("  Alex@Example.COM ", "alex@example.com"),
            ("a+1@x.nl", "a@x.nl"),
            ("A+booking+2@X.nl", "a@x.nl"),
            ("geen-apenstaartje", "geen-apenstaartje"),
        ]:
            with self.subTest(email=email):
                self.assertEqual(email_limit_key(email), key)
