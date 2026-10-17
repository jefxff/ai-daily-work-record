from django.urls import path

from . import views

app_name = "sysbackup"

urlpatterns = [
    path("", views.backup_center, name="center"),
    path("start/", views.api_start_backup, name="api_start_backup"),
    path("restore/", views.api_start_restore, name="api_start_restore"),
    path("status/", views.api_status, name="api_status"),
    path("scan/", views.api_scan, name="api_scan"),
    path("delete/", views.api_delete, name="api_delete"),
    path("download/", views.download, name="download"),
]
