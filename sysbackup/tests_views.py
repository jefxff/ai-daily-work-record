"""sysbackup 视图权限与留痕测试（审计 P1-1 / P3-3）。

备份包 = db.sqlite3 + data.sql + 全部 media/ 附件。此前下载只要 system.read、
恢复只要 system.update，等于在「按模块授权」的 RBAC 上开了横向导出通道；
下载与删除还不进任何操作流水。本文件固化修复后的行为：

1. 只有 system.read 的用户：能看备份中心，但拿不到下载/恢复（视图 403、模板不渲染按钮）；
2. system.update 不再隐含恢复权限（必须显式 system.restore）；
3. 下载/删除均写入 records.OperationLog（操作人 + 包名 + 大小）。
"""

import json
import tempfile
import zipfile
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import Role
from accounts.registry import permission_code
from records.models import OperationLog

from . import services


def _grant(user, *codes):
    role = Role.objects.create(code=f"bk-{user.username}", name=f"备份测试-{user.username}")
    from accounts.models import Permission

    role.permissions.set(Permission.objects.filter(code__in=list(codes)))
    user.rbac_roles.add(role)


def _make_zip(directory):
    p = Path(directory) / "backup_for_test.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("meta.json", "{}")
    return p


def _fake_backups(path):
    """scan_backups 的假返回：让模板里真的循环出备份包行。

    不造这条数据，`{% for b in backups %}` 一行都不渲染，
    "按钮不出现"会因为空列表而恒真，断言等于没写。
    """
    return (
        [{
            "path": str(path),
            "name": path.name,
            "created_at": "2026-10-23 10:00",
            "created_by": "tester",
            "size_text": "1.0 KB",
            "db_tables": 40,
            "db_rows": 1000,
            "media_files": 5,
            "schema_version": 1,
            "mtime": 0,
        }],
        str(path.parent),
    )


class BackupDownloadPermissionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("sync_rbac", verbosity=0)
        # 只有「系统管理·查看」的用户：审计里点名的越权场景
        cls.readonly = User.objects.create_user("bk_readonly", "r@e.com", "X12345678")
        _grant(cls.readonly, permission_code("system", "read"))
        # 内置 admin 角色：默认带 system.backup / system.restore
        cls.admin = User.objects.create_user("bk_admin", "a@e.com", "X12345678")
        cls.admin.rbac_roles.add(Role.objects.get(code="admin"))
        cls.superuser = User.objects.create_superuser("bk_root", "s@e.com", "X12345678")

    def _login(self, user):
        from django.test import Client

        c = Client()
        c.force_login(user)
        return c

    def test_readonly_user_sees_center_but_no_action_buttons(self):
        """仅 system.read：页面 200，但下载/恢复/删除按钮一概不渲染。"""
        with tempfile.TemporaryDirectory() as d:
            p = _make_zip(d)
            with mock.patch.object(services, "scan_backups", return_value=_fake_backups(p)):
                resp = self._login(self.readonly).get(reverse("sysbackup:center"))
            self.assertEqual(resp.status_code, 200)
            # 备份包行确实渲染了（否则下面的断言会因空列表恒真）
            self.assertContains(resp, p.name)
            # 高危动作权限标志全关：静态表格的 {% can %} 与 JS 动态行共用这把锁。
            # （不断言 class 名本身——JS 源码里必然出现这些字面量，无权只是不拼进 DOM。）
            self.assertContains(resp, 'data-restore=""')
            self.assertContains(resp, 'data-backup=""')
            self.assertContains(resp, 'data-delete=""')
            self.assertNotContains(resp, 'data-restore="1"')
            self.assertNotContains(resp, 'data-backup="1"')
            self.assertNotContains(resp, 'data-delete="1"')

    def test_admin_sees_all_action_buttons(self):
        """对照组：admin 角色三个按钮都在（证明上面不是模板写坏导致的恒不渲染）。"""
        with tempfile.TemporaryDirectory() as d:
            p = _make_zip(d)
            with mock.patch.object(services, "scan_backups", return_value=_fake_backups(p)):
                resp = self._login(self.admin).get(reverse("sysbackup:center"))
            self.assertEqual(resp.status_code, 200)
            self.assertContains(resp, 'data-restore="1"')
            self.assertContains(resp, 'data-backup="1"')
            self.assertContains(resp, 'data-delete="1"')

    def test_download_forbidden_for_readonly_user(self):
        with tempfile.TemporaryDirectory() as d:
            p = _make_zip(d)
            resp = self._login(self.readonly).get(
                reverse("sysbackup:download"), {"path": str(p)}
            )
            self.assertEqual(resp.status_code, 403)

    def test_download_allowed_and_logged_for_admin(self):
        """admin 角色可下载，且下载动作进 OperationLog。"""
        with tempfile.TemporaryDirectory() as d:
            p = _make_zip(d)
            with override_settings(BACKUP_ALLOWED_ROOTS=[d]):
                resp = self._login(self.admin).get(
                    reverse("sysbackup:download"), {"path": str(p)}
                )
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(
                    resp["Content-Disposition"], f'attachment; filename="{p.name}"'
                )
                # 消费流以释放文件句柄（否则 Windows 下 TemporaryDirectory 清不掉）
                self.assertTrue(b"PK" in b"".join(resp.streaming_content))
                resp.close()
                size = p.stat().st_size
            log = OperationLog.objects.filter(
                operation_type="下载", object_identifier=p.name
            ).first()
        self.assertIsNotNone(log, "下载备份包必须写操作流水")
        self.assertEqual(log.operator, "bk_admin")
        self.assertIn(str(size), log.change_summary)

    def test_download_allowed_for_superuser(self):
        with tempfile.TemporaryDirectory() as d:
            p = _make_zip(d)
            with override_settings(BACKUP_ALLOWED_ROOTS=[d]):
                resp = self._login(self.superuser).get(
                    reverse("sysbackup:download"), {"path": str(p)}
                )
                self.assertEqual(resp.status_code, 200)
                list(resp.streaming_content)
                resp.close()


class BackupRestorePermissionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("sync_rbac", verbosity=0)
        # 只有 system.update（能改系统配置）但没有 system.restore 的用户
        cls.updater = User.objects.create_user("bk_updater", "u@e.com", "X12345678")
        _grant(cls.updater, permission_code("system", "update"))

    def test_system_update_does_not_imply_restore(self):
        """system.update 不再隐含恢复权限：403 而不是进入业务逻辑。"""
        c = self._login_updater()
        resp = c.post(reverse("sysbackup:api_start_restore"), {"path": "x.zip"})
        self.assertEqual(resp.status_code, 403)

    def test_restore_permission_checked_before_validation(self):
        """有 system.restore 但缺 path：应到达视图业务层返回 400（证明权限放行）。"""
        user = User.objects.create_user("bk_restorer", "rr@e.com", "X12345678")
        _grant(user, permission_code("system", "restore"))
        c = self._login_updater()
        c.force_login(user)
        resp = c.post(reverse("sysbackup:api_start_restore"), {})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("缺少备份包路径", resp.json()["message"])

    def _login_updater(self):
        from django.test import Client

        c = Client()
        c.force_login(self.updater)
        return c


class BackupDeleteLoggingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("sync_rbac", verbosity=0)
        cls.admin = User.objects.create_user("bk_admin2", "a2@e.com", "X12345678")
        cls.admin.rbac_roles.add(Role.objects.get(code="admin"))

    def test_delete_backup_writes_operation_log(self):
        """删除备份包（销毁唯一备份）必须留痕。"""
        from django.test import Client

        with tempfile.TemporaryDirectory() as d:
            p = _make_zip(d)
            with override_settings(BACKUP_ALLOWED_ROOTS=[d]):
                c = Client()
                c.force_login(self.admin)
                resp = c.post(
                    reverse("sysbackup:api_delete"),
                    data=json.dumps({"path": str(p)}),
                    content_type="application/json",
                )
                self.assertEqual(resp.status_code, 200, resp.content)
                self.assertFalse(p.exists())
        log = OperationLog.objects.filter(
            operation_type="删除", object_identifier=p.name
        ).first()
        self.assertIsNotNone(log, "删除备份包必须写操作流水")
        self.assertEqual(log.operator, "bk_admin2")
