from django.contrib import admin

from .models import Attachment


@admin.register(Attachment)
class AttachmentAdmin(admin.ModelAdmin):
    list_display = ("id", "original_name", "category", "size", "uploaded_by", "created_at")
    list_filter = ("content_type", "category")
    search_fields = ("original_name", "remark")
    raw_id_fields = ("content_type", "uploaded_by")
    readonly_fields = ("created_at", "updated_at")
