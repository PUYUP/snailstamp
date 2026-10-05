from django.urls import path
from rest_framework.permissions import AllowAny
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .views import MyAssociationsView

app_name = "tenant-api-v1"

urlpatterns = [
    path("auth/token/", TokenObtainPairView.as_view(permission_classes=[AllowAny]), name="token_obtain_pair"),
    path("auth/token/refresh/", TokenRefreshView.as_view(permission_classes=[AllowAny]), name="token_refresh"),
    path("me/associations/", MyAssociationsView.as_view(), name="my-associations"),
]
