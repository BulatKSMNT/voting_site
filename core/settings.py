"""
Django settings for core project.
"""
from pathlib import Path
from decouple import config

BASE_DIR = Path(__file__).resolve().parent.parent


def env_list(name, default=""):
    raw_value = config(name, default=default)
    return [item.strip() for item in raw_value.split(",") if item.strip()]


SECRET_KEY = config('SECRET_KEY')

DEBUG = config("DEBUG", cast=bool, default=False)

ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", default="127.0.0.1,localhost,web")
TELEGRAM_BOT_USERNAME = config('TELEGRAM_BOT_USERNAME', default='host_battle_bot').lstrip('@')
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS", default="")

SECURE_SSL_REDIRECT = config("SECURE_SSL_REDIRECT", cast=bool, default=False)
SESSION_COOKIE_SECURE = config("SESSION_COOKIE_SECURE", cast=bool, default=False)
CSRF_COOKIE_SECURE = config("CSRF_COOKIE_SECURE", cast=bool, default=False)

if config("USE_X_FORWARDED_PROTO", cast=bool, default=False):
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "same-origin"


INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'voting',
    'rest_framework',
    'rest_framework.authtoken',
]

MIDDLEWARE = [
    'core.middleware.RequestContextMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'core.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'core.context_processors.brand',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'core.wsgi.application'


DB_ENGINE = config('DB_ENGINE', default='sqlite')

if DB_ENGINE == 'postgres':
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': config('POSTGRES_DB'),
            'USER': config('POSTGRES_USER'),
            'PASSWORD': config('POSTGRES_PASSWORD'),
            'HOST': config('POSTGRES_HOST', default='db'),
            'PORT': config('POSTGRES_PORT', default='5432'),
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }


AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


LANGUAGE_CODE = 'ru-RU'
TIME_ZONE = 'Europe/Moscow'
USE_I18N = True
USE_TZ = True


STATIC_URL = '/static/'
STATICFILES_DIRS = [
    BASE_DIR / "static",
]
STATIC_ROOT = BASE_DIR / 'staticfiles'


REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.TokenAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_THROTTLE_CLASSES': ['voting.throttling.ActorThrottle'],
    'DEFAULT_THROTTLE_RATES': {
        'anon': config('API_ANON_RATE', default='60/min'),
        'read': config('API_READ_RATE', default='120/min'),
        'vote': config('API_VOTE_RATE', default='60/min'),
        'manage': config('API_MANAGE_RATE', default='120/min'),
    },
    'EXCEPTION_HANDLER': 'voting.audit.api_exception_handler',
}

DJANGO_API_TOKEN = config('DJANGO_API_TOKEN', default='')
FULL_ADMIN_IDS = {int(value) for value in env_list('FULL_ADMIN_IDS')}
LIMITED_ADMIN_IDS = {int(value) for value in env_list('LIMITED_ADMIN_IDS')}
CACHE_URL = config('CACHE_URL', default='')
if CACHE_URL:
    CACHES = {'default': {'BACKEND': 'django.core.cache.backends.redis.RedisCache', 'LOCATION': CACHE_URL}}

from core.observability import logging_config
LOGGING = logging_config(config('LOG_SERVICE', default='web'))

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
