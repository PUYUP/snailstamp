from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularSwaggerView
from rest_framework.permissions import AllowAny

urlpatterns = [
    # Static endpoints
    path('admin/', admin.site.urls),

    # allauth headless
    path('api/allauth/', include('allauth.headless.urls')),

    # Versioned API endpoints
    path('api/<str:version>/', include('snailstamp.api.urls', namespace='api')),
    # Documentation
    path('api/docs/', SpectacularSwaggerView.as_view(url='/api/v1/schema/', permission_classes=[AllowAny], authentication_classes=[]), name='api-docs'),
]
