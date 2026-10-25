# -*- coding: utf-8 -*-
"""账户 / 通知 / 系统 DRF API 验收测试（补齐页面无 API 缺口）。"""
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Role
from records.models import OperationLog

PWD = "X12345678"


class AdminApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("sync_rbac", verbosity=0)
        cls.root = User.objects.create_superuser("api_root", "api_root@e.com", PWD)

    def setUp(self):
        # 注意：项目有全站 LoginRequiredMiddleware（/api/ 无 Authorization 头一律 401），
        # force_authenticate 只作用于 DRF 层，必须用真实会话登录（force_login）。
        self.c = APIClient(enforce_csrf_checks=False)
        self.c.force_login(self.root)

    # ---------- 用户 ----------
    def test_user_crud_and_actions(self):
        # 创建
        r = self.c.post("/api/accounts/users/", {
            "username": "api_user1", "password": PWD, "is_active": True,
            "profile": {"real_name": "接口用户"},
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        uid = r.data["id"]
        # 列表 / 详情
        self.assertEqual(self.c.get("/api/accounts/users/").status_code, 200)
        self.assertEqual(self.c.get(f"/api/accounts/users/{uid}/").status_code, 200)
        # 编辑
        r = self.c.patch(f"/api/accounts/users/{uid}/",
                         {"profile": {"real_name": "接口用户改"}}, format="json")
        self.assertEqual(r.status_code, 200)
        # 停用 / 启用 / 审核通过 / 拒绝
        self.assertEqual(self.c.post(f"/api/accounts/users/{uid}/toggle/",
                                     {"active": False}, format="json").status_code, 200)
        self.assertEqual(self.c.post(f"/api/accounts/users/{uid}/approve/").status_code, 200)
        self.assertEqual(self.c.post(f"/api/accounts/users/{uid}/reject/").status_code, 200)
        # 重置密码
        r = self.c.post(f"/api/accounts/users/{uid}/reset-password/",
                        {"new_password": "Y12345678y", "confirm": "Y12345678y"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        # 全部动作都有留痕
        self.assertTrue(OperationLog.objects.filter(
            module_type="系统管理", object_identifier__contains="api_user1").count() >= 6)

    def test_non_superuser_denied_user_create(self):
        staff = User.objects.create_user("api_staff", "s@e.com", PWD, is_active=True)
        self.c.force_login(staff)
        r = self.c.post("/api/accounts/users/", {
            "username": "hacker", "password": PWD, "is_active": True}, format="json")
        self.assertEqual(r.status_code, 403)

    # ---------- 角色 ----------
    def test_role_crud(self):
        r = self.c.post("/api/accounts/roles/", {
            "code": "api_role", "name": "接口角色", "is_active": True,
            "permission_codes": [],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        rid = r.data["id"]
        self.assertEqual(self.c.get("/api/accounts/roles/").status_code, 200)
        r = self.c.patch(f"/api/accounts/roles/{rid}/",
                         {"name": "接口角色改"}, format="json")
        self.assertEqual(r.status_code, 200)
        # code 不可修改
        self.assertNotEqual(Role.objects.get(pk=rid).code, "other")
        r = self.c.delete(f"/api/accounts/roles/{rid}/")
        self.assertIn(r.status_code, (200, 204))
        self.assertFalse(Role.objects.filter(pk=rid).exists())
        self.assertTrue(OperationLog.objects.filter(
            module_type="系统管理", object_identifier__contains="api_role").exists())

    # ---------- 会话 / Token ----------
    def test_sessions_and_token(self):
        self.assertEqual(self.c.get("/api/accounts/sessions/").status_code, 200)
        r = self.c.post("/api/accounts/sessions/revoke-others/")
        self.assertEqual(r.status_code, 200)
        # Token 列表（当前无 Token）
        self.assertEqual(self.c.get("/api/accounts/api-tokens/").status_code, 200)

    # ---------- 通知 ----------
    def test_notification_list_and_announce(self):
        r = self.c.get("/api/notifications/")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data["ok"])
        r = self.c.post("/api/notifications/announce/", {
            "title": "接口公告", "content": "内容", "priority": "normal"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertGreaterEqual(r.data["recipients"], 1)
        # 公告送达本人
        r = self.c.get("/api/notifications/")
        self.assertTrue(any(i["title"] == "接口公告" for i in r.data["items"]))
        # 留痕
        self.assertTrue(OperationLog.objects.filter(
            module_type="通知中心", operation_type="发布公告").exists())

    def test_announce_requires_system_create(self):
        staff = User.objects.create_user("api_staff2", "s2@e.com", PWD, is_active=True)
        self.c.force_login(staff)
        r = self.c.post("/api/notifications/announce/", {
            "title": "t", "content": "c"}, format="json")
        self.assertEqual(r.status_code, 403)

    def test_preferences(self):
        r = self.c.get("/api/notifications/preferences/")
        self.assertEqual(r.status_code, 200)
        r = self.c.put("/api/notifications/preferences/",
                       {"agg_window_minutes": 15}, format="json")
        self.assertEqual(r.status_code, 200)

    # ---------- 定时任务 / 系统 ----------
    def test_scheduled_tasks_list_and_run(self):
        r = self.c.get("/api/notifications/tasks/")
        self.assertEqual(r.status_code, 200)
        r = self.c.get("/api/notifications/task-runs/")
        self.assertEqual(r.status_code, 200)
        r = self.c.get("/api/system/health/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("healthy", r.data)
        r = self.c.get("/api/system/perf/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("summary", r.data)

    # ---------- 共享规则层回归（accounts.user_admin） ----------
    def test_role_patch_name_keeps_permissions(self):
        """PATCH 只改名称不得清空权限。

        回归来源：RoleLiteSerializer.to_internal_value 原先无条件注入
        ``permission_codes=[]``，任何局部更新（只改名称/启停）都会把权限清空，
        而页面端不会 —— 同一个角色经两条入口改出两种结果。
        """
        from accounts.models import Permission

        codes = list(Permission.objects.values_list("code", flat=True)[:2])
        r = self.c.post("/api/accounts/roles/", {
            "code": "api_role_keep", "name": "保留权限", "is_active": True,
            "permission_codes": codes,
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        rid = r.data["id"]
        self.assertEqual(Role.objects.get(pk=rid).permissions.count(), len(codes))

        r = self.c.patch(f"/api/accounts/roles/{rid}/", {"name": "改名"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        role = Role.objects.get(pk=rid)
        self.assertEqual(role.name, "改名")
        self.assertEqual(role.permissions.count(), len(codes), "只改名称不应动权限")

    def test_non_superuser_cannot_grant_staff(self):
        """非超管即使有 system.update，也不能把自己/别人提成 is_staff。

        is_staff 决定能否进 Django Admin，属权限根；页面端「非超管表单不渲染该项」，
        API 侧必须等效（由 accounts.user_admin.update_user_account 统一丢弃）。
        """
        admin_role = Role.objects.get(code="admin")
        operator = User.objects.create_user("api_ops", "ops@e.com", PWD, is_active=True)
        operator.rbac_roles.add(admin_role)
        victim = User.objects.create_user("api_victim", "v@e.com", PWD, is_active=True)

        self.c.force_login(operator)
        r = self.c.patch(f"/api/accounts/users/{victim.pk}/",
                         {"is_staff": True}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertFalse(User.objects.get(pk=victim.pk).is_staff,
                         "非超管不得授予 is_staff")

    def test_non_superuser_cannot_touch_superuser(self):
        """非超管（哪怕有 system.update）不能改超管账号 —— 与页面端同一规则。"""
        operator = User.objects.create_user("api_ops2", "ops2@e.com", PWD, is_active=True)
        operator.rbac_roles.add(Role.objects.get(code="admin"))
        self.c.force_login(operator)
        r = self.c.patch(f"/api/accounts/users/{self.root.pk}/",
                         {"is_active": False}, format="json")
        self.assertEqual(r.status_code, 403)
        self.assertTrue(User.objects.get(pk=self.root.pk).is_active)
