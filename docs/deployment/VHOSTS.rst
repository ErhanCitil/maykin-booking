Apache + mod-wsgi configuration
===============================

An example Apache2 vhost configuration follows::

    WSGIDaemonProcess maykinbooking-<target> threads=5 maximum-requests=1000 user=<user> group=staff
    WSGIRestrictStdout Off

    <VirtualHost *:80>
        ServerName my.domain.name

        ErrorLog "/srv/sites/maykinbooking/log/apache2/error.log"
        CustomLog "/srv/sites/maykinbooking/log/apache2/access.log" common

        WSGIProcessGroup maykinbooking-<target>

        Alias /media "/srv/sites/maykinbooking/media/"
        Alias /static "/srv/sites/maykinbooking/static/"

        WSGIScriptAlias / "/srv/sites/maykinbooking/src/maykinbooking/wsgi/wsgi_<target>.py"
    </VirtualHost>


Nginx + uwsgi + supervisor configuration
========================================

Supervisor/uwsgi:
-----------------

.. code::

    [program:uwsgi-maykinbooking-<target>]
    user = <user>
    command = /srv/sites/maykinbooking/env/bin/uwsgi --socket 127.0.0.1:8001 --wsgi-file /srv/sites/maykinbooking/src/maykinbooking/wsgi/wsgi_<target>.py
    home = /srv/sites/maykinbooking/env
    master = true
    processes = 8
    harakiri = 600
    autostart = true
    autorestart = true
    stderr_logfile = /srv/sites/maykinbooking/log/uwsgi_err.log
    stdout_logfile = /srv/sites/maykinbooking/log/uwsgi_out.log
    stopsignal = QUIT

Nginx
-----

.. code::

    upstream django_maykinbooking_<target> {
      ip_hash;
      server 127.0.0.1:8001;
    }

    server {
      listen :80;
      server_name  my.domain.name;

      access_log /srv/sites/maykinbooking/log/nginx-access.log;
      error_log /srv/sites/maykinbooking/log/nginx-error.log;

      location /500.html {
        root /srv/sites/maykinbooking/src/maykinbooking/templates/;
      }
      error_page 500 502 503 504 /500.html;

      location /static/ {
        alias /srv/sites/maykinbooking/static/;
        expires 30d;
      }

      location /media/ {
        alias /srv/sites/maykinbooking/media/;
        expires 30d;
      }

      location / {
        uwsgi_pass django_maykinbooking_<target>;
      }
    }
