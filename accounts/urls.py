"""认证与权限管理路由（/accounts/）。"""

from django.urls import path

from . import views_auth, views_self, views_admin

app_name = "accounts"

urlpatterns = [
    # 认证
    path("login/", views_auth.login_view, name="login"),
    path("logout/", views_auth.logout_view, name="logout"),
    path("register/", views_auth.register_view, name="register"),
    # 自助
    path("password/", views_self.password_change_view, name="password"),
    path("password/done/", views_self.password_done_view, name="password_done"),
    path("sessions/", views_self.session_list_view, name="sessions"),
    path("sessions/<str:session_key>/revoke/", views_self.session_revoke_view, name="session_revoke"),
    path("sessions/revoke-others/", views_self.session_revoke_others_view, name="session_revoke_others"),
    # 系统管理
    path("login-logs/", views_admin.login_log_list_view, name="login_logs"),
    # 用户管理
    path("users/", views_admin.user_list_view, name="user_list"),
    path("users/new/", views_admin.user_create_view, name="user_create"),
    path("users/<int:user_id>/edit/", views_admin.user_edit_view, name="user_edit"),
    path("users/<int:user_id>/toggle/", views_admin.user_toggle_view, name="user_toggle"),
    path("users/<int:user_id>/approve/", views_admin.user_approve_view, name="user_approve"),
    path("users/<int:user_id>/reject/", views_admin.user_reject_view, name="user_reject"),
    path("users/<int:user_id>/reset-password/", views_admin.user_reset_password_view, name="user_reset_password"),
    # 角色管理
    path("roles/", views_admin.role_list_view, name="role_list"),
    path("roles/new/", views_admin.role_create_view, name="role_create"),
    path("roles/<int:role_id>/edit/", views_admin.role_edit_view, name="role_edit"),
    path("roles/<int:role_id>/delete/", views_admin.role_delete_view, name="role_delete"),
]
