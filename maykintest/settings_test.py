# Development settings file.
#
# For any custom settings, create a settings_local.py file. Settings in that
# file will override settings in this file, and the master file.
#
# For example, as a developer, everyone should have the Django debug-toolbar.
# But not every developer wants to use sqlite3. So, you can override the
# database settings in your own settings_local.py.
#
# Maykin Media, 2009
#
# $Id$

from .settings import *

DEBUG = False

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': os.path.join(BASE_DIR, 'db.sqlite3'),
    }
}

JENKINS_TASKS = (
    # 'django_jenkins.tasks.run_pyflakes',
    'django_jenkins.tasks.run_pep8',
)

# django-jenkins needs to know what apps to test
PROJECT_APPS = ['base',
                'form',
]

# ROOT SETTINGS
ROOT_URL = 'http://localhost'

# URL that handles the media served from MEDIA_ROOT. Make sure to use a
# trailing slash if there is a path component (optional in other cases).
# Examples: "http://media.lawrence.com", "http://example.com/media/"
MEDIA_URL = ROOT_URL + '/media/'

INTERNAL_IPS = ('127.0.0.1',)

#####
#
# PLUGIN: Django Extensions / Werkzeug
# http://dev.pocoo.org/projects/werkzeug/
#
INSTALLED_APPS += (
    'django_extensions',
    'django_jenkins',
)

# Email Backend
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

# Maykinmedia client id
GITHUB_CLIENT_ID = "92bd62f5130bbd346b84"
GITHUB_CLIENT_SECRET = "c087c456c86978bd118e9cca4372271368ec9ec8"

BITBUCKET_CLIENT_ID = "TQELTRxtwnY2YaQMmt"
BITBUCKET_CLIENT_SECRET = "NEaksrFKbavtDxR8Rh59j2RYQrXkVt8k"

GOOGLE_CLIENT_ID = "213978940154-mivvnuicq6a45sa5116ghcne0nf8daml.apps.googleusercontent.com"
GOOGLE_CLIENT_SECRET = "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"
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

GOOGLE_FLOW_CONFIG = {
    "client_config": {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uris": [f"{ROOT_URL}:8000/admin/worksheet/google_calendar/?_popup=1"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://accounts.google.com/o/oauth2/token"
        }
    },
    "scopes": GOOGLE_SCOPES,
    "redirect_uri": f"{ROOT_URL}:8000/admin/worksheet/google_calendar/?_popup=1"
}

# Import any machine specific settings.
try:
    from settings_local import *  # noqa
except ImportError:
    pass
