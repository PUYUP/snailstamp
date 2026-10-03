from .base import *

env = environ.Env(
    # set casting, default value
    DEBUG=(bool, False)
)

# Django
DEBUG=env('DJANGO_DEBUG')
ALLOWED_HOSTS=env('DJANGO_ALLOWED_HOSTS').split(',')
SECRET_KEY=env('DJANGO_SECRET_KEY')

# Vault
CERTIFICATE_ENCRYPTION_KEY=env('CERTIFICATE_ENCRYPTION_KEY')