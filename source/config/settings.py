"""Isolated, synthetic-data development prototype; production is not configured."""
import os
from pathlib import Path
from secrets import token_urlsafe

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get('TRAVELHUB_DATA_DIR', BASE_DIR / '.local-state'))
DATA_DIR.mkdir(parents=True, exist_ok=True)
from .restore_guard import require_runtime_allowed
require_runtime_allowed(DATA_DIR / 'prototype.sqlite3')
_secret_file = DATA_DIR / 'django-secret-key'
try:
    with _secret_file.open('x', encoding='utf-8') as _file:
        _secret_file.chmod(0o600)
        _file.write(token_urlsafe(48))
except FileExistsError:
    pass
SECRET_KEY = _secret_file.read_text(encoding='utf-8').strip()
if not SECRET_KEY:
    raise RuntimeError('Local signing key is empty')
DEBUG = False
ALLOWED_HOSTS = ['127.0.0.1', 'localhost', 'testserver']
INSTALLED_APPS = ['django.contrib.auth', 'django.contrib.contenttypes',
                  'django.contrib.sessions', 'django.contrib.messages',
                  'django.contrib.staticfiles', 'presentations', 'travelcards', 'speech_workflow',
                  'worksheet_reviews', 'inquiries', 'telegram_gateway']
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'config.middleware.PrivateShareResponses',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]
ROOT_URLCONF = 'config.urls'
TEMPLATES = [{'BACKEND': 'django.template.backends.django.DjangoTemplates',
              'DIRS': [], 'APP_DIRS': True, 'OPTIONS': {'context_processors': [
                  'django.template.context_processors.request',
                  'django.contrib.auth.context_processors.auth',
                  'django.contrib.messages.context_processors.messages',
              ]}}]
WSGI_APPLICATION = 'config.wsgi.application'
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3',
                         'NAME': DATA_DIR / 'prototype.sqlite3', 'OPTIONS': {'timeout': 20}}}
AUTH_PASSWORD_VALIDATORS = []  # Local synthetic accounts only; no public registration.
LANGUAGE_CODE = 'ru'
TIME_ZONE = 'Europe/Samara'
USE_I18N = True
USE_TZ = True
STATIC_URL = '/static/'
STATIC_ROOT = DATA_DIR / 'static'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
LOGIN_URL = '/accounts/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = LOGIN_URL
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Strict'
CSRF_COOKIE_SAMESITE = 'Strict'
X_FRAME_OPTIONS = 'DENY'
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'same-origin'
# Secure cookies/TLS and production hosting are deliberately not configured for loopback demo.
LOGGING = {
    'version': 1, 'disable_existing_loggers': False,
    'filters': {'redact_links': {'()': 'config.logging.RedactShareLinks'}},
    'handlers': {'console': {'class': 'logging.StreamHandler', 'filters': ['redact_links']}},
    'loggers': {'django.server': {'handlers': ['console'], 'level': 'INFO', 'propagate': False}},
}

CSRF_FAILURE_VIEW = "config.errors.csrf_failure"

# Explicit Binding injection only; no tokens, accounts, network client or live webhook configured.
TELEGRAM_GATEWAY = None

# Local synthetic voice-to-inquiry seam only. No provider can be enabled by this flag.
VOICE_INQUIRY_BOUNDARY_ENABLED = False
