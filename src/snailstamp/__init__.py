INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
]

# Expose the Celery application as ``snailstamp`` for ``celery -A snailstamp``.
from .celery_app import app as celery_app  # noqa: F401,E402
