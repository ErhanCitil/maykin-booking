==============
Change history
==============

All notable changes to this project are documented in this file. The format
is based on `Keep a Changelog`_.

.. _Keep a Changelog: https://keepachangelog.com/en/1.1.0/


Unreleased: phase 1 "Foundation"
================================

Deploying this release needs extra steps (pre-flight queries, migrations 0009
to 0013, new environment variables): see ``docs/releases/phase-1.rst``.

Added
-----

* Availability per night: every room type has a number of rooms
  (``Room.inventory``, default 5), minus the bookings and the room blocks
  (``RoomBlock``).
* Booking status (pending payment, confirmed, cancelled, expired) and payment
  status (at the hotel, open, paid, refunded), with a log of every change
  (``OrderStatusChange``). The price of every night is stored with the booking
  (``OrderNight``).
* "Mijn boeking" (``/boeking/<token>/``): the guest's booking page, with the
  PDF and free cancellation until 00:00 on the day before arrival.
* Confirmation mail with the PDF attached, and a cancellation mail. Every mail
  is logged (``EmailLog``).
* Check-in and check-out filter on the city page. The checkout only offers
  rooms that are free for the whole stay.
* Dashboard: booking list (``/dashboard/boekingen/``) with filters, quick
  filters and a CSV export; booking page with the status log and the mail
  log; cancelling by staff with a reason; sending a mail again (at most once
  per 2 minutes); room blocks per room type.
* Rate limits on the checkout (5 per hour per IP address, 3 per day per e-mail
  address) and the PDF download (20 per minute per IP address).
* Settings ``SITE_URL``, ``DEFAULT_FROM_EMAIL``, ``BOOKING_CONTACT_EMAIL``,
  ``BOOKING_LIMIT_PER_IP_PER_HOUR``, ``BOOKING_LIMIT_PER_EMAIL_PER_DAY``,
  ``BOOKING_PDF_LIMIT_PER_IP_PER_MINUTE``, ``BOOKING_TRUSTED_PROXY_COUNT``,
  ``SITE_URL_REQUIRE_HTTPS``, ``SECURE_SSL_REDIRECT`` and
  ``SECURE_HSTS_SECONDS``.
* Management command ``expire_pending_orders``, for cron from phase 2 on.
* CI fails on model changes without a migration
  (``makemigrations --check``).

Changed
-------

* Hotels and rooms with bookings can no longer be deleted.
* The settings that build on ``production.py`` require an https
  ``SITE_URL`` (unless ``SITE_URL_REQUIRE_HTTPS=no``), redirect http to https
  and send HSTS.

Removed
-------

* The URLs ``success/`` and ``orderpdf/``. After booking, the guest goes to
  "Mijn boeking".

Fixed
-----

* ``makemigrations`` no longer creates a migration for the absolute
  ``STATIC_ROOT`` of the machine that runs it (migration 0009).

Security
--------

* The API is read-only.
* Sentry is set up without IP addresses, cookies, user data, request bodies or
  local variables, and booking tokens are removed from the URLs in its events.
* Guest names and addresses cannot contain line breaks, control characters or
  bidi overrides.
