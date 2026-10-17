from django.urls import path

from . import views

app_name = "notifications"

urlpatterns = [
    path("", views.notification_list, name="list"),
    path("api/unread/", views.unread_api, name="unread_api"),
    path("read/<int:pk>/", views.notification_read, name="read"),
    path("read-all/", views.notification_read_all, name="read_all"),
    path("revoke/<int:pk>/", views.notification_revoke, name="revoke"),
    path("revoke-log/", views.revoke_log, name="revoke_log"),
    path("preferences/", views.preference_edit, name="preferences"),
    path("announce/", views.announce, name="announce"),
    path("tasks/<int:pk>/update/", views.scheduled_task_update, name="task_update"),
    path("tasks/<int:pk>/run/", views.scheduled_task_run, name="task_run"),
]
