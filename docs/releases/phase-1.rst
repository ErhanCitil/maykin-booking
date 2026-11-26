.. _release_phase_1:

=====================
Phase 1: "Foundation"
=====================

Phase 1 adds availability per night, booking and payment statuses, "Mijn
boeking", booking mails and booking management in the dashboard. See
``CHANGELOG.rst`` for the full list. This page describes how to deploy it on an
existing installation.

The old code cannot run against the new database schema, and the new code
cannot run against the old one. Plan a short downtime.


Before the deploy
=================

Make a backup of the database.

Run these queries on the database. Both must return no rows.

Orders without a valid period. Migration 0011 stops with an error that lists
them; fix or delete them first:

.. code-block:: sql

    SELECT id FROM base_order
    WHERE start_date IS NULL OR end_date IS NULL OR end_date <= start_date;

Duplicate booking tokens. Migration 0010 adds a unique index on
``base_order.token`` and fails on duplicates:

.. code-block:: sql

    SELECT token, COUNT(*) FROM base_order
    GROUP BY token HAVING COUNT(*) > 1;

Set the new environment variables (details in ``INSTALL.rst``). The settings
that build on ``production.py`` do not load without ``SITE_URL``, so
``manage.py migrate`` needs it too.

* ``SITE_URL`` (required): the public URL of the site, without a trailing
  slash. Must start with ``https://``, unless ``SITE_URL_REQUIRE_HTTPS=no``.
  This also applies to staging and test: give them an https URL, or set
  ``SITE_URL_REQUIRE_HTTPS=no`` if they really run on http.
* ``BOOKING_TRUSTED_PROXY_COUNT``: the number of reverse proxies in front of
  the site (see "Rate limits behind a proxy").
* ``DEFAULT_FROM_EMAIL`` and ``BOOKING_CONTACT_EMAIL``: sender and reply-to
  address of the booking mails. Check the defaults
  (``maykinbooking@example.com`` and ``info@maykinbooking.nl``).
* ``EMAIL_HOST``, ``EMAIL_PORT``, ``EMAIL_HOST_USER``, ``EMAIL_HOST_PASSWORD``
  and ``EMAIL_USE_TLS``: the SMTP server for the mails.
* ``SECURE_SSL_REDIRECT`` (default on) and ``SECURE_HSTS_SECONDS`` (default
  one year). The proxy must pass ``X-Forwarded-Proto: https``
  (``SECURE_PROXY_SSL_HEADER``); without it, the redirect to https loops.


Deploy and migrate
==================

From migration 0010 on, the old code cannot create orders: the new columns
``status``, ``payment_status`` and ``language`` are NOT NULL without a
database default. Migration 0012 makes ``total_price``, ``start_date`` and
``end_date`` NOT NULL as well.

1. Stop the web workers, or switch on a maintenance page.
2. Deploy the new code and install the requirements.
3. Run the migrations:

   .. code-block:: bash

       $ python src/manage.py migrate

4. Start the web workers with the new code.

The Docker image runs ``migrate`` when it starts (``bin/docker_start.sh``):
stop the old containers first, and run the queries above before starting the
new image.

Migration 0011 fills in the existing orders: one ``OrderNight`` per night at
the *current* price of the room (older prices are not stored anywhere), the
total price, and a log entry "Migratie fase 1" for the status and the payment
status. Existing orders get the status "confirmed" and the payment status
"at the hotel".


After the migration
===================

Every existing room type now has 5 rooms. Check it, and set the real numbers
on the room page in the dashboard ("Aantal kamers"):

.. code-block:: sql

    SELECT inventory, COUNT(*) FROM base_room GROUP BY inventory;

Before phase 1 there was no limit, so future nights can have more bookings
than rooms. Nothing is cancelled automatically. This query lists those nights;
the dashboard also warns about them when you lower the number of rooms:

.. code-block:: sql

    SELECT o.room_id, n.date, COUNT(*) AS booked, r.inventory
    FROM base_ordernight n
    JOIN base_order o ON o.id = n.order_id
    JOIN base_room r ON r.id = o.room_id
    WHERE o.status = 'confirmed' AND n.date >= CURRENT_DATE
    GROUP BY o.room_id, n.date, r.inventory
    HAVING COUNT(*) > r.inventory;


Rate limits behind a proxy
==========================

The rate limits count per client address: per IPv4 address, or per IPv6 /64
network. By default (``BOOKING_TRUSTED_PROXY_COUNT=0``) the client address is
``REMOTE_ADDR`` and ``X-Forwarded-For`` is ignored. Behind a reverse proxy,
make sure the site sees the real client address, otherwise all guests share
one limit: 5 bookings per hour for everybody.

* nginx with ``uwsgi_pass``: ``include uwsgi_params;`` passes the client
  address (``$remote_addr``) as ``REMOTE_ADDR``. Keep
  ``BOOKING_TRUSTED_PROXY_COUNT=0``.
* An HTTP ``proxy_pass`` (for example to the Docker image, which runs uwsgi
  with ``--http``): set ``BOOKING_TRUSTED_PROXY_COUNT`` to the number of
  proxies that add to ``X-Forwarded-For`` (usually ``1``). The site then takes
  the N-th address from the right, which a client cannot fake.
* With a load balancer in front of nginx, either count it in
  ``BOOKING_TRUSTED_PROXY_COUNT`` or use the nginx realip module
  (``set_real_ip_from`` and ``real_ip_header``) so that ``$remote_addr`` is the
  client.

If no valid client address can be found, the IP limit is skipped and a warning
is logged; the e-mail limit still applies.

The limits are counted in the default cache: Redis with the production
settings (``CACHE_DEFAULT``). If Redis cannot be reached, nothing is limited.


Cron
====

``expire_pending_orders`` sets bookings that wait for payment to "expired"
once their hold has passed. Phase 1 confirms every booking at once, so the
job is only needed from phase 2 on. Then run it every minute, with the same
environment (``DJANGO_SETTINGS_MODULE`` and the other variables) as the web
workers:

.. code-block:: text

    * * * * * cd /path/to/maykin-booking && env/bin/python src/manage.py expire_pending_orders --verbosity 0


Open points before production
=============================

These points are still open and must be done before the site goes live:

* Tailwind is loaded from ``cdn.tailwindcss.com``
  (``templates/components/head.html``). Build it locally, and add a Content
  Security Policy; there is none yet.
* The staff dashboard (``/dashboard/``) has no two-factor authentication. Only
  the Django admin has it.
* Dependency upgrades: Django 3.2 is end of life.
* Hotel terms: staff can enter HTML, which the terms page shows unescaped.
  Sanitize it with an allowlist (for example nh3); this needs a new
  dependency.
* SMTP over TLS: ``EMAIL_USE_TLS`` is off by default.
* Booking tokens in URLs (``/boeking/<token>/``) are removed in Sentry, but not
  yet in the nginx access logs and in Elastic APM.
