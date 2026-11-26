==============
Change history
==============

All notable changes to this project are documented in this file. The format
is based on `Keep a Changelog`_.

.. _Keep a Changelog: https://keepachangelog.com/en/1.1.0/


Unreleased: phase 2 "Money"
===========================

Deploying this release needs extra steps (migrations, new environment
variables, cron, roles, seller details): see ``docs/releases/phase-2.rst``.

Added
-----

* Price rules: a weekend surcharge for the Friday and Saturday night,
  high-season periods with a surcharge, and a discount from a number of
  nights on. Every night stores its base price and surcharges.
* Discount codes: a percentage or a fixed amount, a period of validity and a
  maximum number of uses. The checkout allows 10 code attempts per hour per
  IP address.
* Invoices: issued when a booking is confirmed, numbered ``YYYY-NNNNN``
  without gaps per year, and never changed. A cancelled booking gets a credit
  note. The PDFs are stored in ``PRIVATE_MEDIA_ROOT`` and are never served as
  files. VAT is calculated back from the price, with rates per country
  (``VatRate``, in the admin); the Netherlands has 9% up to 2025 and 21% from
  2026. Without seller details or a VAT rate, an invoice gets the watermark
  "Testfactuur – niet geldig". Tourist tax is not on the invoice.
* Online payment with iDEAL via Mollie (test keys only), or the fake provider
  in development and CI. An online booking holds the room for
  ``PAYMENT_HOLD_MINUTES`` (15). It has a webhook and a return URL.
* Refunds: when a booking that was paid online is cancelled, and
  automatically for a payment that comes too late, twice or for another
  amount. A refund that fails is retried by the cron and from the dashboard.
* Dashboard: price rules, discount codes, invoices (list, PDF, CSV),
  customers per e-mail address (with CSV), and a report of occupancy and
  revenue per hotel. Every booking page has a payment and refund panel.
* Staff roles as groups: "Receptie" and "Beheer".
* Guest field "Woonplaats"; country per hotel.
* Management command ``sync_payments``, for cron every minute next to
  ``expire_pending_orders``.
* Settings ``INVOICE_SELLER_NAME``, ``INVOICE_SELLER_ADDRESS``,
  ``INVOICE_SELLER_KVK``, ``INVOICE_SELLER_VAT_ID``, ``PRIVATE_MEDIA_ROOT``,
  ``MOLLIE_API_KEY``, ``PAYMENT_FAKE_PROVIDER`` and ``PAYMENT_HOLD_MINUTES``.

Changed
-------

* The dashboard checks permissions per page. Existing staff members get the
  role "Beheer" (migration ``accounts/0002``).
* An expired booking that was never paid gets the payment status "unpaid".
* ``load_demo_data`` gives every hotel the country of its city. It also
  deletes invoices, payments and refunds, and refuses outside development and
  CI and when there are real invoices.
* The production settings refuse ``DEBUG``, ``PAYMENT_FAKE_PROVIDER`` and live
  Mollie keys.


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
