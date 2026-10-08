from django.urls import include, path

urlpatterns = [
    path("", include("snailstamp.apps.ledger.api.v1.urls")),
]
