from django.urls import path, include

app_name = "tenant-api-v1"

urlpatterns = [
    path("associations/", include("snailstamp.apps.tenant.api.v1.association.urls")),
]
