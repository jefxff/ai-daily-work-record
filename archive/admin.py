from django.contrib import admin

from .models import Archive, BorrowRecord, Cabinet, Category


@admin.register(Cabinet)
class CabinetAdmin(admin.ModelAdmin):
    list_display = ("code", "location", "layers", "cells_per_layer", "created_at")
    search_fields = ("code", "location")
    list_filter = ("layers",)


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "parent", "sort_order")
    list_filter = ("parent",)
    ordering = ("sort_order",)


@admin.register(Archive)
class ArchiveAdmin(admin.ModelAdmin):
    list_display = (
        "code",
        "name",
        "cabinet",
        "layer",
        "cell",
        "category",
        "creator",
        "created_at",
    )
    list_filter = ("cabinet", "category")
    search_fields = ("code", "name", "tags")
    raw_id_fields = ("creator",)


@admin.register(BorrowRecord)
class BorrowRecordAdmin(admin.ModelAdmin):
    list_display = ("archive", "borrower", "borrow_time", "due_time", "return_time", "status")
    list_filter = ("status",)
    search_fields = ("archive__code", "archive__name", "borrower__username")
    raw_id_fields = ("archive", "borrower")
