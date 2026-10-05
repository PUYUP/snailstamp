"""Project-level URL composition for API-wide endpoints and app-owned APIs."""
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView
from rest_framework.permissions import AllowAny

from . import schema  # noqa: F401  (register the SimpleJWT OpenAPI auth scheme)

app_name = "api"

urlpatterns = [
    path("schema/", SpectacularAPIView.as_view(permission_classes=[AllowAny], authentication_classes=[]),
         name="schema"),
    path("", include("snailstamp.apps.tenant.api.urls")),
    path("", include("snailstamp.apps.ledger.api.urls")),
]
