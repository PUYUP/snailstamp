from django.urls import path
from .views import ListCreateEntryView, RetrieveUpdateDestroyEntryView

urlpatterns = [
    path('', ListCreateEntryView.as_view(), name='create-entry'),
    path('<int:pk>/', RetrieveUpdateDestroyEntryView.as_view(), name='retrieve-update-entry'),
]