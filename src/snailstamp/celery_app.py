import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "snailstamp.core.settings.development")

app = Celery("snailstamp")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
