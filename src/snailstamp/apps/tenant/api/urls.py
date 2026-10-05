from django.urls import include, path

urlpatterns = [
    path("", include("snailstamp.apps.tenant.api.v1.urls")),
]
