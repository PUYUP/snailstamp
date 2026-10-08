from django.urls import path, include

from .views import (
    ActionListView,
    CollectionHistoryView,
    CollectionListView,
    KindListView,
    QueueTransactionView,
    QueuedTransactionDetailView,
)

app_name = "ledger-api-v1"

urlpatterns = [
    path("kinds/", KindListView.as_view(), name="kind-list"),
    path("actions/", ActionListView.as_view(), name="action-list"),
    path("items/", CollectionListView.as_view(), name="item-list"),
    path("items/<int:collection_id>/history/", CollectionHistoryView.as_view(), name="item-history"),
    path("transactions/", QueueTransactionView.as_view(), name="transaction-create"),
    path("transactions/<int:transaction_id>/", QueuedTransactionDetailView.as_view(),
         name="transaction-detail"),

    path("entries/", include("snailstamp.apps.ledger.api.v1.entry.urls")),
    path("collections/", include("snailstamp.apps.ledger.api.v1.collection.urls"))
]
