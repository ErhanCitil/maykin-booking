.. _release_phase_2:

================
Phase 2: "Money"
================

Phase 2 adds price rules and discount codes, invoices and credit notes, online
payment with iDEAL (Mollie, test mode only), refunds, staff roles and new
dashboard pages (price rules, discount codes, invoices, customers, report).
See ``CHANGELOG.rst`` for the full list. This page describes how to deploy it
on an installation that runs phase 1.

As with phase 1, the old code cannot run against the new database schema.
Plan a short downtime.


Before the deploy
=================

Make a backup of the database.

Migration 0016 adds a check that the total price of an order is not negative.
This query must return no rows:

.. code-block:: sql

    SELECT id FROM base_order WHERE total_price < 0;

Set the new environment variables (details in ``INSTALL.rst``) before
``manage.py migrate``: the settings check ``MOLLIE_API_KEY``,
``PAYMENT_FAKE_PROVIDER`` and ``DEBUG`` when they load.

* ``INVOICE_SELLER_NAME``, ``INVOICE_SELLER_ADDRESS``, ``INVOICE_SELLER_KVK``
  and ``INVOICE_SELLER_VAT_ID``: the seller on the invoices. While one of them
  is empty, every invoice gets the watermark "Testfactuur – niet geldig".
  Invoices never change, so an invoice issued without these details stays a
  test invoice.
* ``PRIVATE_MEDIA_ROOT``: where the invoice PDFs are stored; see below.
* ``MOLLIE_API_KEY`` (optional): a Mollie test key. Without it, online payment
  is not offered and guests pay at the hotel.
* ``PAYMENT_HOLD_MINUTES`` (optional, default 15).
* ``PAYMENT_FAKE_PROVIDER`` must be off and ``DEBUG`` must be off: the
  production settings do not start otherwise. Note that ``dotenv.example``
  turns the fake provider on.


Invoice files (PRIVATE_MEDIA_ROOT)
==================================

The invoice PDFs contain personal data and are part of the administration.

* Give ``PRIVATE_MEDIA_ROOT`` its own directory or volume, outside
  ``MEDIA_ROOT`` and ``STATIC_ROOT``. The site only hands the files out
  through views that check access, so the web server must not serve this
  directory: no nginx ``location`` and no alias for it.
* Back it up with the database; invoices must be kept for 7 years. A missing
  or changed PDF is rendered again from the database, and the problem is
  logged.
* The Docker image has the volume ``/app/private_media`` for it. Put it on
  persistent storage.
* Staging: ``bin/live2staging.sh`` only copies the invoice PDFs when both
  ``PROD_PRIVATE_MEDIA_PATH`` and ``STAGING_PRIVATE_MEDIA_PATH`` are set. Leave
  them unset unless staging may hold the real invoices; without them, staging
  renders an invoice from its database copy when it is asked for.


Deploy and migrate
==================

Stop the old version before ``migrate``. The new columns of migration 0014
(on ``base_order``, ``base_ordernight`` and ``base_hotel``) are NOT NULL
without a database default, and migration 0016 makes
``base_ordernight.base_price`` NOT NULL, so the old code cannot create orders
anymore. Migration 0017 also replaces the CHECK constraint on the payment
status.

1. Stop the web workers and the cron jobs, or switch on a maintenance page.
2. Deploy the new code and install the requirements.
3. Run the migrations:

   .. code-block:: bash

       $ python src/manage.py migrate

4. Start the web workers and the cron jobs (see below) with the new code.

The Docker image runs ``migrate`` when it starts (``bin/docker_start.sh``):
stop the old containers first.

What the migrations do with existing data:

* ``base/0014``: every existing hotel gets the country NL.
* ``base/0015``: every existing night gets its price as base price, without
  surcharges.
* ``base/0018``: expired bookings with the payment status "open" get "unpaid".
* ``billing/0002``: the VAT rates of the Netherlands (9% up to 2025, 21% from
  1 January 2026), and the price rules with every percentage at 0.
* ``accounts/0002``: the groups "Receptie" and "Beheer". Every existing staff
  member who is not a superuser gets "Beheer".

Bookings from before phase 2 have no invoice. Cancelling one gives no credit
note, and they do not count in the revenue of the report.


After the migration
===================

Before guests book, set up everything an invoice depends on. An invoice is
fixed when it is issued.

**Hotel countries.** The country of the hotel decides the VAT rate. Correct
the hotels outside the Netherlands on their page in the dashboard ("Land").
This query lists the hotels with the country NL, per city:

.. code-block:: sql

    SELECT c.name AS city, h.id, h.name
    FROM base_hotel h JOIN base_city c ON c.id = h.city_id
    WHERE h.country = 'NL'
    ORDER BY c.name, h.name;

**VAT rates.** Add a rate for every other country with hotels in the Django
admin (``/admin/``, "btw-tarieven"). Only the superuser can manage them. With
the production settings, the admin asks for a two-factor code. Without a rate,
the invoice is a test invoice, and its revenue is left out of the report.

**Seller details and an accountant.** Check the seller details on a first
invoice, and have an accountant review an invoice and a credit note before
the site goes live: the required details, the numbering and the VAT that is
calculated back from the price. Tourist tax is not on the invoice; it is paid
at the hotel.

**Price rules.** All percentages are 0 after the migration. Set them in the
dashboard ("Prijsregels").

**Staff roles.** Give every staff member one of the groups in the Django admin
(user, "Groepen"):

* "Receptie": see bookings, cancel them, send their mails again and retry
  refunds.
* "Beheer": everything of the reception, plus hotels and rooms, price rules,
  discount codes, invoices, customers and the report.

Existing staff got "Beheer": move reception staff to "Receptie". The
superuser can do everything. A staff member without a role only sees the
dashboard home page.

**Staff who leave.** A user who ever issued an invoice or a credit note (for
example by cancelling a booking) cannot be deleted: ``Invoice.created_by``
protects them. Deactivate the user instead (clear "Actief", that is
``is_active=False``).


Online payment (Mollie)
=======================

Only Mollie's test mode is supported. The settings refuse a ``MOLLIE_API_KEY``
that does not start with ``test_``, and the production settings refuse
``live_`` keys explicitly.

* Mollie calls the webhook (``/betalen/webhook/``) only when ``SITE_URL``
  starts with ``https://``. Without it, the return URL and the cron
  (``sync_payments``) handle the payments.
* A booking that waits for online payment holds its room for
  ``PAYMENT_HOLD_MINUTES``. While its payment is still open at Mollie, it
  expires only 15 minutes after the hold. A payment that comes in after that,
  twice or for another amount is refunded automatically.

Going live needs a code change (the checks above), a Mollie account with a
verified business, and an https ``SITE_URL`` for the webhook.


Cron
====

Run both commands every minute, with the same environment
(``DJANGO_SETTINGS_MODULE`` and the other variables) as the web workers:

.. code-block:: text

    * * * * * cd /path/to/maykin-booking && env/bin/python src/manage.py sync_payments --verbosity 0
    * * * * * cd /path/to/maykin-booking && env/bin/python src/manage.py expire_pending_orders --verbosity 0

* ``sync_payments`` asks the provider about open payments that are older than
  2 minutes, and sends failed refunds again (up to 5 attempts; staff can also
  retry from the booking page).
* ``expire_pending_orders`` lets bookings expire whose hold has passed, after
  checking their payments with the provider. Their room and discount code are
  free again.

Without the cron, a payment is only processed when the guest comes back to the
return URL (or through the webhook). The room of a booking is free again once
its hold has passed, but the booking stays "waiting for payment" and keeps its
discount code. Failed refunds are then only retried from the dashboard.


Open points before production
=============================

These points are still open and must be done before the site goes live:

* Retention of invoices: keep them for 7 years, then delete them together
  with the personal data of the booking (AVG). There is no purge yet.
* Tailwind is loaded from ``cdn.tailwindcss.com``. Build it locally, and add
  a Content Security Policy; there is none yet.
* Dependency upgrades: Django 3.2 is end of life.
* SMTP over TLS: ``EMAIL_USE_TLS`` is off by default.
* The booking token (``/boeking/<token>/``) and the payment reference
  (``/betalen/<reference>/terug/``, which leads to "Mijn boeking") work as
  passwords in the URL. They are removed in Sentry, but not yet in the nginx
  access logs and in Elastic APM.
* Still open from phase 1: hotel terms are shown as unescaped HTML. Sanitize
  them with an allowlist.

Decided: the staff dashboard gets no two-factor authentication (decision of
the client).
