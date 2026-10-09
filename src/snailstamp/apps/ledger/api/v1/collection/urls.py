from django.urls import path
from .views import ListCreateInventoryView

urlpatterns = [
    path('inventories/', ListCreateInventoryView.as_view(), name='list-create-inventory'),
]