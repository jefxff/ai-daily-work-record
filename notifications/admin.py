from django.contrib import admin

from .models import (
    Notification,
    NotificationDelivery,
    NotificationPreference,
    NotificationTemplate,
)


class NotificationDeliveryInline(admin.TabularInline):
    model = NotificationDelivery
    extra = 0
    readonly_fields = ["channel", "status", "detail", "attempts", "created_at", "updated_at"]
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(NotificationTemplate)
class NotificationTemplateAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "category", "is_active", "updated_at")
    list_filter = ("category", "is_active")
    search_fields = ("code", "name", "description")


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = (
        "title", "recipient", "category", "status", "is_read",
        "agg_count", "retry_count", "created_at",
    )
    list_filter = ("status", "category", "is_read")
    search_fields = ("title", "content", "recipient__username")
    readonly_fields = ("dedup_key", "agg_key", "created_at", "updated_at")
    inlines = [NotificationDeliveryInline]


@admin.register(NotificationPreference)
class NotificationPreferenceAdmin(admin.ModelAdmin):
    list_display = ("user", "agg_window_minutes", "quiet_start", "quiet_end", "updated_at")


@admin.register(NotificationDelivery)
class NotificationDeliveryAdmin(admin.ModelAdmin):
    list_display = ("notification", "channel", "status", "attempts", "updated_at")
    list_filter = ("channel", "status")
