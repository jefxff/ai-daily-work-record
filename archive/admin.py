from django.contrib import admin

from .models import Archive, AnnualReview, BorrowRecord, Cabinet, Category, License, LicenseBorrowRecord


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
        "counterpart",
        "department",
        "location_code",
        "sign_date",
        "expire_date",
        "creator",
        "created_at",
    )
    list_filter = ("cabinet", "category", "department", "security_level")
    search_fields = ("code", "name", "counterpart", "contract_no", "tags", "description", "location_code", "handler")
    raw_id_fields = ("creator",)


@admin.register(BorrowRecord)
class BorrowRecordAdmin(admin.ModelAdmin):
    list_display = ("archive", "borrower", "borrow_time", "due_time", "return_time", "status")
    list_filter = ("status",)
    search_fields = ("archive__code", "archive__name", "borrower")
    raw_id_fields = ("archive",)


@admin.register(License)
class LicenseAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "holder", "issuer", "issue_date", "expire_date", "next_review_date", "created_at")
    list_filter = ("review_cycle",)
    search_fields = ("code", "name", "holder", "issuer", "remark")
    raw_id_fields = ("creator",)


@admin.register(AnnualReview)
class AnnualReviewAdmin(admin.ModelAdmin):
    list_display = ("license", "review_date", "result", "reviewer", "new_expire_date")
    list_filter = ("result",)
    search_fields = ("license__code", "license__name", "reviewer")
    raw_id_fields = ("license",)


@admin.register(LicenseBorrowRecord)
class LicenseBorrowRecordAdmin(admin.ModelAdmin):
    list_display = ("license", "borrower", "borrow_time", "due_time", "return_time", "status")
    list_filter = ("status",)
    search_fields = ("license__code", "license__name", "borrower")
    raw_id_fields = ("license",)
