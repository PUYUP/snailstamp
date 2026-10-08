from django.urls import path
from .views import ListCreateEntryView, RetrieveUpdateEntryView

urlpatterns = [
    path('', ListCreateEntryView.as_view(), name='create-entry'),
    path('<int:pk>/', RetrieveUpdateEntryView.as_view(), name='retrieve-update-entry'),
]