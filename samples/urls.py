from django.urls import path

from . import views

app_name = "samples"

urlpatterns = [
    # 看板
    path("", views.dashboard, name="dashboard"),

    # 样品主档
    path("samples/", views.sample_list, name="list"),
    path("samples/new/", views.sample_create, name="create"),
    path("samples/<int:pk>/", views.sample_detail, name="detail"),
    path("samples/<int:pk>/edit/", views.sample_edit, name="edit"),
    path("samples/<int:pk>/delete/", views.sample_delete, name="delete"),
    path("samples/<int:pk>/restore/", views.sample_restore, name="restore"),
    path("samples/<int:pk>/terminate/", views.sample_terminate, name="terminate"),

    # 试样记录（多轮试产）
    path("samples/<int:pk>/tests/new/", views.test_create, name="test_create"),
    path("samples/<int:pk>/tests/<int:record_pk>/edit/", views.test_edit, name="test_edit"),
    path(
        "samples/<int:pk>/tests/<int:record_pk>/delete/",
        views.test_delete,
        name="test_delete",
    ),

    # 小批量记录（多批验证）
    path("samples/<int:pk>/batches/new/", views.batch_create, name="batch_create"),
    path(
        "samples/<int:pk>/batches/<int:record_pk>/edit/",
        views.batch_edit,
        name="batch_edit",
    ),
    path(
        "samples/<int:pk>/batches/<int:record_pk>/delete/",
        views.batch_delete,
        name="batch_delete",
    ),

    # 供应商类型切换（AJAX）
    path("samples/<int:pk>/supplier-type/", views.supplier_type_update, name="supplier_type_update"),

    # 证明材料（业务流程关键节点凭证：试样报告/金蝶截图/邮件凭证等）
    path(
        "samples/<int:pk>/proofs/<str:stage>/upload/",
        views.proof_upload,
        name="proof_upload",
    ),
    path(
        "samples/<int:pk>/proofs/<int:attachment_pk>/delete/",
        views.proof_delete,
        name="proof_delete",
    ),

    # 供应商档案
    path("partners/", views.partner_list, name="partner_list"),
    path("partners/new/", views.partner_create, name="partner_create"),
    path("partners/<int:pk>/", views.partner_detail, name="partner_detail"),
    path("partners/<int:pk>/edit/", views.partner_edit, name="partner_edit"),
    path("partners/<int:pk>/delete/", views.partner_delete, name="partner_delete"),

    # 审厂记录（多轮现场审核，按供应商维度管理）
    path("partners/<int:pk>/audits/new/", views.audit_create, name="audit_create"),
    path("partners/<int:pk>/audits/<int:record_pk>/edit/", views.audit_edit, name="audit_edit"),
    path("partners/<int:pk>/audits/<int:record_pk>/delete/", views.audit_delete, name="audit_delete"),

    # Excel 导出
    path("export/samples.xlsx", views.export_samples, name="export"),
]
