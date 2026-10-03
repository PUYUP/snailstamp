from django.contrib import admin

from .models import Collection, Entry


class ReadOnlyLedgerAdmin(admin.ModelAdmin):
    """Ledger hanya boleh ditulis lewat ledger.services; admin cuma untuk melihat."""
    show_full_result_count = False        # COUNT(*) di tabel partisi besar itu mahal

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Entry)
class EntryAdmin(ReadOnlyLedgerAdmin):
    list_display = ("id", "kind_id", "issuer_id", "issuer_member_id", "supply", "minted_count", "created_at")
    ordering = ("-id",)


@admin.register(Collection)
class CollectionAdmin(ReadOnlyLedgerAdmin):
    list_display = ("id", "serial_no", "entry_id", "owner_id", "state", "last_seq", "updated_at")
    search_fields = ("=serial_no",)       # exact match: LIKE '%..%' menyapu 64 partisi
    ordering = ("-id",)
