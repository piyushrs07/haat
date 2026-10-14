"""
Django settings for the Haat (vendorapp) project.

Environment variables (set these on Render -> Environment):
    SECRET_KEY            required in production (render.yaml generates one)
    DATABASE_URL          required in production (Postgres connection string)
    DEBUG                 optional; "True"/"False". Defaults to False on Render, True locally.
    ALLOWED_HOSTS         optional, comma separated extra hosts (custom domain)
    CSRF_TRUSTED_ORIGINS  optional, comma separated extra origins, e.g. https://www.example.com
    BREVO_API_KEY         required for OTP / notification emails
    DEFAULT_FROM_EMAIL    optional, e.g. "Haat <you@yourdomain.com>" (sender must be verified in Brevo)
    ADMIN_EMAIL           optional, where new-registration alerts are sent
    DJANGO_SUPERUSER_USERNAME / DJANGO_SUPERUSER_EMAIL / DJANGO_SUPERUSER_PASSWORD
                          optional; creates the /admin/ login on deploy (see build.sh)
"""

import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def env_list(name):
    return [v.strip() for v in os.environ.get(name, '').split(',') if v.strip()]


# Render sets RENDER=true on every service it runs.
ON_RENDER = bool(os.environ.get('RENDER'))

# Secure by default in production: DEBUG is only on when explicitly asked for,
# or when running locally with no DEBUG variable set.
_debug_env = os.environ.get('DEBUG')
if _debug_env is None:
    DEBUG = not ON_RENDER
else:
    DEBUG = _debug_env.strip().lower() in ('1', 'true', 'yes', 'on')

SECRET_KEY = os.environ.get('SECRET_KEY')
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = 'django-insecure-local-development-key-do-not-use-in-production'
    else:
        raise ImproperlyConfigured(
            'The SECRET_KEY environment variable is required when DEBUG is off. '
            'Set it in Render -> Environment.'
        )

# ---------- Hosts / CSRF ----------
ALLOWED_HOSTS = ['127.0.0.1', 'localhost', '[::1]'] + env_list('ALLOWED_HOSTS')
CSRF_TRUSTED_ORIGINS = env_list('CSRF_TRUSTED_ORIGINS')

_render_host = os.environ.get('RENDER_EXTERNAL_HOSTNAME')
if _render_host:
    ALLOWED_HOSTS.append(_render_host)
    CSRF_TRUSTED_ORIGINS.append(f'https://{_render_host}')
elif ON_RENDER:
    # Fallback if RENDER_EXTERNAL_HOSTNAME is ever missing.
    ALLOWED_HOSTS.append('.onrender.com')
    CSRF_TRUSTED_ORIGINS.append('https://*.onrender.com')

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'core',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'vendorapp.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'core.context_processors.notifications',
            ],
        },
    },
]

WSGI_APPLICATION = 'vendorapp.wsgi.application'

# ---------- Database ----------
# Production: DATABASE_URL (Postgres). Local default: a SQLite file, so nothing
# needs installing. To keep using local MySQL, set
#   DATABASE_URL=mysql://root:root@localhost:3306/local_vendor_app
# and `pip install mysqlclient` on your own machine only.
DATABASES = {
    'default': dj_database_url.config(
        default=f'sqlite:///{BASE_DIR / "db.sqlite3"}',
        conn_max_age=600,
        conn_health_checks=True,
    )
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Kolkata'   # Goa - keeps "estimated delivery" dates correct for local users
USE_I18N = True
USE_TZ = True

# ---------- Static files / uploads ----------
STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
try:
    STATIC_ROOT.mkdir(exist_ok=True)     # avoids a harmless WhiteNoise warning before collectstatic has run
except OSError:
    pass

# Uploaded photos are stored in the database (see core/storage.py) because
# Render's disk is wiped on every redeploy/restart.
STORAGES = {
    'default': {'BACKEND': 'core.storage.DatabaseStorage'},
    'staticfiles': {
        'BACKEND': (
            'django.contrib.staticfiles.storage.StaticFilesStorage' if DEBUG
            else 'whitenoise.storage.CompressedManifestStaticFilesStorage'
        ),
    },
}
WHITENOISE_MANIFEST_STRICT = False   # never 500 on a missing hashed-static entry

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# Upload limits (phone photos are big; core/security.py enforces the per-file cap)
MAX_UPLOAD_BYTES = 12 * 1024 * 1024
MAX_EXTRA_PHOTOS = 8
DATA_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024     # non-file form data
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024

# ---------- Email (Brevo HTTPS API; Render blocks outbound SMTP on free plans) ----------
BREVO_API_KEY = os.environ.get('BREVO_API_KEY', '')
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', 'Haat <marketplacehaat@gmail.com>')
# Receives "new seller / customer registered" alerts
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', 'rajsinghpiyush07@gmail.com')

# ---------- Security (production) ----------
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')   # Render terminates TLS
if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SECURE_REDIRECT_EXEMPT = [r'^healthz/?$']        # Render's health probe
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(os.environ.get('SECURE_HSTS_SECONDS', 31536000))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = False
    SECURE_HSTS_PRELOAD = False
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SAMESITE = 'Lax'
SECURE_REFERRER_POLICY = 'same-origin'
X_FRAME_OPTIONS = 'DENY'

# ---------- Logging (shows up in Render's log stream) ----------
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {'simple': {'format': '%(levelname)s %(name)s: %(message)s'}},
    'handlers': {'console': {'class': 'logging.StreamHandler', 'formatter': 'simple'}},
    'root': {'handlers': ['console'], 'level': 'INFO'},
    'loggers': {'django.request': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False}},
}

# Used to version the service worker cache; Render provides the git commit.
ASSET_VERSION = (os.environ.get('RENDER_GIT_COMMIT') or 'dev')[:10]

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
