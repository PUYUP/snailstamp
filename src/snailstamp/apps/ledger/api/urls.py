from django.urls import include, path

urlpatterns = [
    path("ledger/", include("snailstamp.apps.ledger.api.v1.urls")),
]
