from django.contrib import admin

from .models import MediaObject


@admin.register(MediaObject)
class MediaObjectAdmin(admin.ModelAdmin):
    """Hanya lihat. Mengubah/menghapus baris di sini akan memisahkan tabel dari S3 dan ledger;
    gunakan ledger_media.services (erase_media, dll)."""
    list_display = ("id", "collection_id", "log_seq", "association_id", "uploaded_by_id", "content_type",
                    "size", "status", "created_at")
    list_filter = ("status", "content_type")
    search_fields = ("=id", "filename")
    ordering = ("-created_at",)
    show_full_result_count = False

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
