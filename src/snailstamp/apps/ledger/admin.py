from django.contrib import admin

from .models import BlockSignature, Collection, Entry, SealerKey, SealerKeyAuditLog


class ReadOnlyLedgerAdmin(admin.ModelAdmin):
    """Ledger hanya boleh ditulis lewat ledger.services; admin cuma untuk melihat."""
    show_full_result_count = False        # COUNT(*) di tabel partisi besar itu mahal

    def has_add_permission(self, request):
        return True

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


@admin.register(SealerKey)
class SealerKeyAdmin(ReadOnlyLedgerAdmin):
    """Ubah key lewat ledger.services (register/rotate/revoke) supaya tercatat di audit log."""
    list_display = ("name", "region", "status", "public_key", "valid_from", "valid_until", "deactivated_at")
    list_filter = ("status", "region")
    search_fields = ("name", "=public_key")


@admin.register(SealerKeyAuditLog)
class SealerKeyAuditLogAdmin(ReadOnlyLedgerAdmin):
    list_display = ("timestamp", "event_type", "sealer_name", "region", "rotation_reason",
                    "block_at_rotation", "actor_email")
    list_filter = ("event_type", "region")
    search_fields = ("sealer_name", "=sealer_id", "actor_email")
    ordering = ("-timestamp",)


@admin.register(BlockSignature)
class BlockSignatureAdmin(ReadOnlyLedgerAdmin):
    list_display = ("block_id", "sealer_id", "region", "signed_at")
    list_filter = ("region",)
    ordering = ("-block_id",)
