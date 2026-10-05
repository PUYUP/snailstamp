import dj_database_url
from .base import *

env = environ.Env(
    # set casting, default value
    DEBUG=(bool, False)
)

# Django
DEBUG=env('DJANGO_DEBUG')
ALLOWED_HOSTS=env('DJANGO_ALLOWED_HOSTS').split(',')
SECRET_KEY=env('DJANGO_SECRET_KEY')
PG_DATABASE_URL = env("PG_DATABASE_URL", default="")

# Vault
CERTIFICATE_ENCRYPTION_KEY=env('CERTIFICATE_ENCRYPTION_KEY')

# Database
# https://docs.djangoproject.com/en/6.1/ref/settings/#databases

DATABASES = {
    "default": dj_database_url.config(
        default=PG_DATABASE_URL,
        conn_max_age=600,
    )
}