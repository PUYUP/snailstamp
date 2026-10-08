from django.urls import path, include
from rest_framework.permissions import AllowAny
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

app_name = "users-api-v1"

urlpatterns = [
    # from third-party
    path('auth/google/', include('allauth.socialaccount.providers.google.urls')),

    # apps endpoints
    path("auth/token/", TokenObtainPairView.as_view(permission_classes=[AllowAny]), name="token_obtain_pair"),
    path("auth/token/refresh/", TokenRefreshView.as_view(permission_classes=[AllowAny]), name="token_refresh"),
]