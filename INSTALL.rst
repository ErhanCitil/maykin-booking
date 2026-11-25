============
Installation
============

The project is developed in Python using the `Django framework`_. There are 2
sections below, focussing on developers and running the project using Docker.

.. _Django framework: https://www.djangoproject.com/


Development
===========


Prerequisites
-------------

You need the following libraries and/or programs:

* `Python`_ 3.10
* Python `Virtualenv`_ and `Pip`_
* `PostgreSQL`_ 14 (optional for local development, see step 5 below)
* `Node.js`_
* `npm`_

.. _Python: https://www.python.org/
.. _Virtualenv: https://virtualenv.pypa.io/en/stable/
.. _Pip: https://packaging.python.org/tutorials/installing-packages/#ensure-pip-setuptools-and-wheel-are-up-to-date
.. _PostgreSQL: https://www.postgresql.org
.. _Node.js: http://nodejs.org/
.. _npm: https://www.npmjs.com/


Getting started
---------------

Developers can follow the following steps to set up the project on their local
development machine. Run all ``manage.py`` commands from the repository root.

1. Navigate to the location where you want to place your project.

2. Get the code:

   .. code-block:: bash

       $ git clone git@github.com:ErhanCitil/maykin-booking.git
       $ cd maykin-booking

3. Install all required (backend) libraries. ``maykin-django-two-factor-auth``
   only installs with a pip version below 24.1, so install that first:

   .. code-block:: bash

       $ virtualenv env
       $ source env/bin/activate
       $ pip install "pip<24.1"
       $ pip install -r requirements/dev.txt

   **Note:** ``python bootstrap.py dev`` automates these steps and also sets
   ``DJANGO_SETTINGS_MODULE`` in ``env/bin/activate``. It upgrades pip to the
   latest version first, though, so it runs into the pip limit above.

4. Install all required (frontend) libraries and build static files.

   .. code-block:: bash

       $ npm install
       $ npm run build

5. Create a ``.env`` file with your database settings, based on
   ``dotenv.example``:

   .. code-block:: bash

       $ cp dotenv.example .env

   Remove ``DB_PORT`` or give it a value: it is read as a number, so an empty
   value fails.

   **Tip:** to run the project without PostgreSQL, use SQLite by setting
   these values in ``.env``. The database file is created in the repository
   root. Do not base ``local.py`` on ``local_example.py`` in that case, as it
   overrides the database settings.

   .. code-block:: bash

       DB_ENGINE=django.db.backends.sqlite3
       DB_NAME=db.sqlite3

6. Collect statics and create the initial database tables:

   .. code-block:: bash

       $ python src/manage.py collectstatic --link
       $ python src/manage.py migrate

7. Create a superuser to access the management interface:

   .. code-block:: bash

       $ python src/manage.py createsuperuser

8. You can now run your installation and point your browser to the address
   given by this command:

   .. code-block:: bash

       $ python src/manage.py runserver


**Note:** If you are making local, machine specific, changes, add them to
``src/maykinbooking/conf/local.py``. You can base this file on the
example file included in the same directory.


Update installation
-------------------

When updating an existing installation:

1. Activate the virtual environment:

   .. code-block:: bash

       $ cd maykin-booking
       $ source env/bin/activate

2. Update the code and libraries:

   .. code-block:: bash

       $ git pull
       $ pip install -r requirements/dev.txt
       $ npm install
       $ npm run build

3. Update the statics and database:

   .. code-block:: bash

       $ python src/manage.py collectstatic --link
       $ python src/manage.py migrate


Testsuite
---------

To run the test suite:

.. code-block:: bash

    $ python src/manage.py test maykinbooking

Configuration via environment variables
---------------------------------------

A number of common settings/configurations can be modified by setting
environment variables. You can persist these in the ``.env`` file in the
repository root or as part of the ``(post)activate`` of your virtualenv.

* ``SECRET_KEY``: the secret key to use. A default is set in ``dev.py``

* ``DB_ENGINE``: database backend. Defaults to
  ``django.db.backends.postgresql``; use ``django.db.backends.sqlite3`` for
  SQLite.
* ``DB_NAME``: name of the database for the project. Defaults to ``maykinbooking``.
* ``DB_USER``: username to connect to the database with. Defaults to ``maykinbooking``.
* ``DB_PASSWORD``: password to use to connect to the database. Defaults to ``maykinbooking``.
* ``DB_HOST``: database host. Defaults to ``localhost``
* ``DB_PORT``: database port. Defaults to ``5432``.

* ``SENTRY_DSN``: the DSN of the project in Sentry. If set, enabled Sentry SDK as
  logger and will send errors/logging to Sentry. If unset, Sentry SDK will be
  disabled.

Docker
======

The easiest way to get the project started is by using `Docker Compose`_.

1. Clone or download the code from `Github`_ in a folder like
   ``maykin-booking``:

   .. code-block:: bash

       $ git clone git@github.com:ErhanCitil/maykin-booking.git
       Cloning into 'maykin-booking'...
       ...

       $ cd maykin-booking

2. Start the database and web services:

   .. code-block:: bash

       $ docker compose up -d

   It can take a while before everything is done. Even after starting the web
   container, the database might still be migrating. You can always check the
   status with:

   .. code-block:: bash

       $ docker compose logs -f web

3. Create an admin user:

   .. code-block:: bash

       $ docker compose exec web python src/manage.py createsuperuser
       Username: admin
       ...
       Superuser created successfully.

4. Point your browser to ``http://localhost:8000/`` to access the project's
   management interface with the credentials used in step 3.

   If you are using ``Docker Machine``, you need to point your browser to the
   Docker VM IP address. You can get the IP address by doing
   ``docker-machine ls`` and point your browser to
   ``http://<ip>:8000/`` instead (where the ``<ip>`` is shown below the URL
   column):

   .. code-block:: bash

       $ docker-machine ls
       NAME      ACTIVE   DRIVER       STATE     URL
       default   *        virtualbox   Running   tcp://<ip>:<port>

5. To shutdown the services, use ``docker compose down`` and to clean up your
   system you can run ``docker system prune``.

.. _Docker Compose: https://docs.docker.com/compose/install/
.. _Github: https://github.com/ErhanCitil/maykin-booking


More Docker
-----------

If you just want to run the project as a Docker container and connect to an
external database, you can build and run the ``Dockerfile`` and pass several
environment variables. See ``src/maykinbooking/conf/docker.py`` for
all settings.

.. code-block:: bash

    $ docker build -t maykinbooking .
    $ docker run \
        -p 8000:8000 \
        -e SECRET_KEY=... \
        -e ALLOWED_HOSTS=... \
        -e DATABASE_NAME=... \
        -e DATABASE_USER=... \
        -e DATABASE_PASSWORD=... \
        -e DATABASE_HOST=... \
        --name maykinbooking \
        maykinbooking

    $ docker exec -it maykinbooking python src/manage.py createsuperuser


Settings
========

All settings for the project can be found in
``src/maykinbooking/conf``.
The file ``local.py`` overwrites settings from the base configuration.


Commands
========

Commands can be executed using:

.. code-block:: bash

    $ python src/manage.py <command>

``load_demo_data`` fills the database with demo data: 6 cities, 201 real
hotels from OpenStreetMap and freely licensed photos.

**Warning:** this command is destructive. It first deletes all cities, hotels,
rooms, bookings and reviews; users are kept. It is meant for development and
demo environments, not for production, and refuses to run when ``ENVIRONMENT``
is ``production``. It asks for confirmation (type ``ja``) unless you pass
``--noinput``:

.. code-block:: bash

    $ python src/manage.py load_demo_data
    $ python src/manage.py load_demo_data --noinput

See ``src/maykinbooking/base/demo_data/README.md`` for the source and license
of the data.

Other project commands:

* ``createinitialsuperuser``: creates a superuser, or does nothing if that
  username already exists. See ``--help`` for the password options.
* ``dataframe``: downloads ``city.csv`` and ``hotel.csv`` from a Maykin Media
  server and creates the cities and hotels, each with a single, double and
  family room. Only works on a database without cities.
* ``fileimport``: imports the cities and hotels from the CSV files in
  ``Case Maykin Media - Django - CSV data/``, with one double room per hotel.
  Only works on a database without cities.
* ``email_csv``: meant to e-mail a CSV of all hotels to a hard-coded address;
  the attached ``data.csv`` is currently empty.
* ``clear_cache``: clears the default cache, or the one given with ``--alias``.

See `Django framework commands`_ for all default commands, or type
``python src/manage.py --help``.

.. _Django framework commands: https://docs.djangoproject.com/en/dev/ref/django-admin/#available-commands
