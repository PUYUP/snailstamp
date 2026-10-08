from django.urls import path
from .views import (
    MyAssociationsView,
    ListCreateAssociationView,
    RetrieveUpdateDestroyAssociationView,
)

urlpatterns = [
    # path("me/", MyAssociationsView.as_view(), name="my-associations"),
    path("", ListCreateAssociationView.as_view(), name="list-create-association"),
    path("<uuid:pk>/", RetrieveUpdateDestroyAssociationView.as_view(), name="retrieve-update-delete-association"),
]
