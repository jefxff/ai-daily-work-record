"""档案管理模块 URL 配置。"""

from django.urls import path

from . import views

app_name = "archive"

urlpatterns = [
    # 总览
    path("", views.overview, name="overview"),

    # 档案柜
    path("cabinets/new/", views.cabinet_create, name="cabinet_create"),
    path("cabinets/<int:pk>/edit/", views.cabinet_edit, name="cabinet_edit"),
    path("cabinets/<int:pk>/delete/", views.cabinet_delete, name="cabinet_delete"),

    # 档案
    path("archives/new/", views.archive_create, name="archive_create"),
    path("archives/<int:pk>/", views.archive_detail, name="archive_detail"),
    path("archives/<int:pk>/edit/", views.archive_edit, name="archive_edit"),
    path("archives/<int:pk>/move/", views.archive_move, name="archive_move"),
    path("archives/<int:pk>/delete/", views.archive_delete, name="archive_delete"),
    path("archives/<int:pk>/qrcode.png", views.qrcode, name="qrcode"),

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
    path("import/", views.import_archives, name="import"),
    path("export/", views.export_archives, name="export"),

    # 操作日志
    path("logs/", views.log_list, name="log_list"),
]
