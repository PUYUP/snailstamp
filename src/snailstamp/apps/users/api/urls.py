from django.urls import include, path

urlpatterns = [
    path("", include("snailstamp.apps.users.api.v1.urls")),
]
