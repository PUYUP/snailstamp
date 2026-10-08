from django.urls import path
from .views import CreateEntryView

urlpatterns = [
    path('', CreateEntryView.as_view(), name='create-entry'),
]