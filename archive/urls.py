"""档案管理模块 URL 配置。"""

from django.urls import path

from . import views

app_name = "archive"

urlpatterns = [
    # 总览
    path("", views.overview, name="overview"),

    # 台账
    path("ledgers/new/", views.ledger_create, name="ledger_create"),
    path("ledgers/export/", views.ledger_batch_export, name="ledger_batch_export"),
    path("ledgers/import/", views.ledger_batch_import, name="ledger_batch_import"),
    path("ledgers/<int:pk>/", views.ledger_detail, name="ledger_detail"),
    path("ledgers/<int:pk>/edit/", views.ledger_edit, name="ledger_edit"),
    path("ledgers/<int:pk>/move/", views.ledger_move, name="ledger_move"),
    path("ledgers/<int:pk>/delete/", views.ledger_delete, name="ledger_delete"),
    path("ledgers/<int:pk>/export/", views.ledger_export, name="ledger_export"),
    path("ledgers/<int:pk>/import/", views.ledger_import, name="ledger_import"),

    # 档案柜
    path("cabinets/", views.cabinet_list, name="cabinet_list"),
    path("cabinets/new/", views.cabinet_create, name="cabinet_create"),
    path("cabinets/<int:pk>/edit/", views.cabinet_edit, name="cabinet_edit"),
    path("cabinets/<int:pk>/delete/", views.cabinet_delete, name="cabinet_delete"),

    # 档案
    path("archives/new/", views.archive_create, name="archive_create"),
    path("archives/<int:pk>/", views.archive_detail, name="archive_detail"),
    path("archives/<int:pk>/edit/", views.archive_edit, name="archive_edit"),
    path("archives/<int:pk>/move/", views.archive_move, name="archive_move"),
    path("archives/<int:pk>/renewal/", views.archive_set_renewal, name="archive_set_renewal"),
    path("archives/<int:pk>/delete/", views.archive_delete, name="archive_delete"),
    path("archives/<int:pk>/qrcode.png", views.qrcode, name="qrcode"),

    # 档案电子附件（一条档案可挂多个文件）
    path("archives/<int:pk>/files/upload/", views.archive_files_upload, name="archive_files_upload"),
    path("files/<int:pk>/delete/", views.archive_file_delete, name="archive_file_delete"),
    path("files/<int:pk>/thumb/", views.archive_file_thumb, name="archive_file_thumb"),

    # 类别
    path("categories/", views.category_list, name="category_list"),
    path("categories/new/", views.category_create, name="category_create"),
    path("categories/<int:pk>/edit/", views.category_edit, name="category_edit"),
    path("categories/<int:pk>/delete/", views.category_delete, name="category_delete"),

    # 借阅
    path("borrows/", views.borrow_list, name="borrow_list"),
    path("archives/<int:archive_pk>/borrow/", views.borrow_create, name="borrow_create"),
    path("borrows/<int:pk>/return/", views.borrow_return, name="borrow_return"),

    # 导入导出
    path("import/template/", views.import_template, name="import_template"),
    path("import/", views.import_archives, name="import_archives"),
    path("export/", views.export_archives, name="export"),
    # 表级通用导入导出（导出 → Excel 修改 → 导入更新）
    path("tables/<str:table>/export/", views.table_export, name="table_export"),
    path("tables/<str:table>/template/", views.table_template, name="table_template"),
    path("tables/<str:table>/import/", views.table_import, name="table_import"),

    # ---- 证照管理子模块 ----
    path("licenses/", views.license_list, name="license_list"),
    path("licenses/import/", views.import_licenses, name="license_import"),
    path("licenses/new/", views.license_create, name="license_create"),
    path("licenses/<int:pk>/", views.license_detail, name="license_detail"),
    path("licenses/<int:pk>/edit/", views.license_edit, name="license_edit"),
    path("licenses/<int:pk>/delete/", views.license_delete, name="license_delete"),
    path("licenses/<int:license_pk>/review/", views.annual_review_create, name="annual_review_create"),
    path("licenses/<int:license_pk>/borrow/", views.license_borrow_create, name="license_borrow_create"),
    path("licenses/borrows/", views.license_borrow_list, name="license_borrow_list"),
    path("licenses/borrows/<int:pk>/return/", views.license_borrow_return, name="license_borrow_return"),
]
