from django.urls import path

from . import views

app_name = "attachments"

urlpatterns = [
    # 上传：app_label/model/pk 定位业务对象，权限按对象归属模块判定
    path(
        "upload/<str:app_label>/<str:model>/<int:pk>/",
        views.attachment_upload,
        name="upload",
    ),
    path("<int:pk>/delete/", views.attachment_delete, name="delete"),
    path("<int:pk>/thumb/", views.attachment_thumb, name="thumb"),
]
