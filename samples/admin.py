from django.contrib import admin

from .models import AuditRecord, BatchRecord, Partner, ProofAttachment, Sample, TestRecord


@admin.register(Partner)
class PartnerAdmin(admin.ModelAdmin):
    list_display = ("partner_code", "name", "contact_person", "contact_info", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("partner_code", "name", "contact_person", "contact_info")
    list_editable = ("status",)


class ReadonlyTabularInline(admin.TabularInline):
    """子记录在主档页仅作查看；新增/编辑必须走业务页面以触发状态机与留痕。"""

    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class TestRecordInline(ReadonlyTabularInline):
    model = TestRecord
    fields = ("round_no", "test_no", "test_date", "review_conclusion", "report_code", "notified", "operator")
    readonly_fields = fields
    ordering = ("-round_no",)


class BatchRecordInline(ReadonlyTabularInline):
    model = BatchRecord
    fields = ("round_no", "batch_no", "ordered", "batch_result", "kingdee_entered", "unit_price", "operator")
    readonly_fields = fields
    ordering = ("-round_no",)


class ProofAttachmentInline(ReadonlyTabularInline):
    model = ProofAttachment
    fields = ("stage", "original_name", "uploaded_by", "created_at", "remark")
    readonly_fields = fields
    ordering = ("-created_at",)


@admin.register(Sample)
class SampleAdmin(admin.ModelAdmin):
    list_display = (
        "sample_no", "material_name", "model", "sample_date",
        "status", "registrar", "is_deleted",
    )
    list_filter = ("status", "is_deleted", "sample_date")
    search_fields = ("sample_no", "material_name", "model", "supplier__name")
    date_hierarchy = "sample_date"
    inlines = [TestRecordInline, BatchRecordInline, ProofAttachmentInline]


@admin.register(TestRecord)
class TestRecordAdmin(admin.ModelAdmin):
    list_display = ("test_no", "sample", "round_no", "test_date", "review_conclusion", "report_code", "notified", "operator")
    list_filter = ("review_conclusion", "notified", "test_date")
    search_fields = ("test_no", "sample__sample_no", "report_code")


@admin.register(BatchRecord)
class BatchRecordAdmin(admin.ModelAdmin):
    list_display = ("batch_no", "sample", "round_no", "ordered", "batch_result", "kingdee_entered", "unit_price", "operator")
    list_filter = ("batch_result", "ordered", "kingdee_entered")
    search_fields = ("batch_no", "sample__sample_no", "alternative_model")


@admin.register(ProofAttachment)
class ProofAttachmentAdmin(admin.ModelAdmin):
    list_display = ("original_name", "get_stage_display", "sample", "uploaded_by", "created_at")
    list_filter = ("stage", "created_at")
    search_fields = ("original_name", "sample__sample_no", "remark", "uploaded_by")
    date_hierarchy = "created_at"
    readonly_fields = ("sample", "stage", "file", "original_name", "uploaded_by", "created_at", "updated_at", "remark")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(AuditRecord)
class AuditRecordAdmin(admin.ModelAdmin):
    list_display = ("audit_no", "partner", "round_no", "audit_date", "audit_result", "auditor")
    list_filter = ("audit_result", "audit_date")
    search_fields = ("audit_no", "partner__name", "auditor")
