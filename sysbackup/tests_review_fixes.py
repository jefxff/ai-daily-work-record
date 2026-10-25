# -*- coding: utf-8 -*-
"""审查 A/B 批修复的回归测试。

覆盖：
1. 备份轮转（H7）：保留最近 N 天 + 每周 1 份，不误删 safety_snapshots；
2. 备份密钥迁移（S1）：旧位置（备份目录）→ 新位置（用户配置目录），旧副本删除；
3. 维护模式（S2/B2）：恢复期间非超管写请求 503，超管放行；
4. 导出闸门（H5/B3）：并发占满 429、超行数 413；
5. 定时任务种子（H6）：审计清理与库维护默认启用。
"""
import os
import tempfile
import time
from pathlib import Path

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings

from accounts.models import Role
from notifications.models import ScheduledTask
from sysbackup import services as bk
from sysbackup import views as sysbackup_views

PWD = "X12345678"


class BackupRotationTests(TestCase):
    """审查 H7：备份轮转策略。"""

    def test_rotate_keeps_recent_and_weekly(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            now = time.time()
            # 造 30 个每日包（每天一个，越靠前越新）
            for i in range(30):
                f = dest / f"backup_202601{i + 1:02d}_020000{bk.ENCRYPTED_SUFFIX}"
                f.write_bytes(b"x")
                os.utime(f, (now - i * 86400, now - i * 86400))
            # 干扰项：安全快照与用户自放文件绝不能被删
            safety = dest / "safety_snapshots"
            safety.mkdir()
            (safety / "before_restore_x.sqlite3").write_bytes(b"keep")
            (dest / "manual.zip").write_bytes(b"keep")

            removed = bk._rotate_backups(dest)

            remaining = sorted(p.name for p in dest.glob(f"backup_*{bk.ENCRYPTED_SUFFIX}"))
            self.assertEqual(removed, 30 - len(remaining))
            self.assertLessEqual(len(remaining), 14 + 8)
            self.assertGreaterEqual(len(remaining), 14)
            self.assertTrue((safety / "before_restore_x.sqlite3").exists())
            self.assertTrue((dest / "manual.zip").exists())

    def test_rotate_noop_when_under_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            for i in range(3):
                (dest / f"backup_2026010{i + 1}_020000{bk.ENCRYPTED_SUFFIX}").write_bytes(b"x")
            self.assertEqual(bk._rotate_backups(dest), 0)
            self.assertEqual(len(list(dest.glob("backup_*"))), 3)


class BackupKeyMigrationTests(TestCase):
    """审查 S1：密钥与备份目录分离，且旧密钥能自动迁移（否则历史包解不开）。"""

    def test_key_migrates_from_backup_dir_and_legacy_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            new_key = root / "keys" / ".backup_key"
            backup_dir = root / "backups"
            backup_dir.mkdir()
            legacy = backup_dir / ".backup_key"
            from cryptography.fernet import Fernet

            key_value = Fernet.generate_key()
            legacy.write_bytes(key_value)

            with override_settings():
                import unittest.mock as mock

                with mock.patch.object(bk, "BACKUP_KEY_FILE", new_key), \
                        mock.patch.object(bk, "_LEGACY_BACKUP_KEY_FILES", [legacy]):
                    resolved = bk._key_file()

            self.assertEqual(resolved, new_key)
            self.assertEqual(new_key.read_bytes(), key_value)
            self.assertFalse(legacy.exists(), "迁移成功后旧副本应删除，真正做到分离")
            # 用迁移后的密钥能解开用旧密钥加密的内容
            from cryptography.fernet import Fernet as F

            token = F(key_value).encrypt(b"payload")
            self.assertEqual(F(new_key.read_bytes()).decrypt(token), b"payload")


class MaintenanceModeTests(TestCase):
    """审查 S2/B2：恢复期间拒绝非超管写入。"""

    @classmethod
    def setUpTestData(cls):
        call_command("sync_rbac", verbosity=0)
        cls.root = User.objects.create_superuser("mm_root", "r@e.com", PWD)
        cls.staff = User.objects.create_user("mm_staff", "s@e.com", PWD, is_active=True)
        cls.staff.rbac_roles.add(Role.objects.get(code="staff"))

    def tearDown(self):
        cache.delete(bk.MAINTENANCE_CACHE_KEY)

    def test_non_superuser_write_blocked_during_restore(self):
        self.client.force_login(self.staff)
        bk.set_maintenance_mode(True)
        resp = self.client.post("/daily_record/add/", {})
        self.assertEqual(resp.status_code, 503)

    def test_read_not_blocked(self):
        self.client.force_login(self.staff)
        bk.set_maintenance_mode(True)
        resp = self.client.get("/daily_record/")
        self.assertNotEqual(resp.status_code, 503)

    def test_superuser_write_allowed(self):
        self.client.force_login(self.root)
        bk.set_maintenance_mode(True)
        resp = self.client.get("/daily_record/")
        self.assertNotEqual(resp.status_code, 503)

    def test_flag_cleared_after_restore(self):
        bk.set_maintenance_mode(True)
        self.assertTrue(bk.in_maintenance_mode())
        bk.set_maintenance_mode(False)
        self.assertFalse(bk.in_maintenance_mode())


class ExportGuardTests(TestCase):
    """审查 H5/B3：导出并发闸门与行数上限。"""

    @classmethod
    def setUpTestData(cls):
        call_command("sync_rbac", verbosity=0)
        cls.root = User.objects.create_superuser("eg_root", "r@e.com", PWD)

    def test_busy_returns_429(self):
        from work_record import export_guard

        exported = []
        for _ in range(export_guard.DEFAULT_MAX_CONCURRENCY):
            slot = export_guard.ExportSlot()
            self.assertTrue(slot.__enter__())
            exported.append(slot)
        try:
            self.client.force_login(self.root)
            resp = self.client.get("/decoration/export/")
            self.assertEqual(resp.status_code, 429)
        finally:
            for slot in exported:
                slot.__exit__(None, None, None)

    def test_row_limit_returns_413(self):
        from records.models import Decoration

        for i in range(5):
            Decoration.objects.create(
                project_name=f"项目{i}", constructor="施工方",
                start_date="2026-01-01", final_price="1000.00",
            )
        self.client.force_login(self.root)
        with self.settings(EXPORT_MAX_ROWS=2):
            resp = self.client.get("/decoration/export/")
        self.assertEqual(resp.status_code, 413)
        self.assertIn("超过单次上限", resp.content.decode("utf-8"))


class SeededMaintenanceTaskTests(TestCase):
    """审查 H6/W6：清理与维护任务随迁移种子默认启用。"""

    def test_tasks_seeded_and_enabled(self):
        codes = {"cleanup_operation_log", "db_maintenance"}
        seeded = ScheduledTask.objects.filter(code__in=codes)
        self.assertEqual(seeded.count(), 2)
        for task in seeded:
            self.assertTrue(task.enabled, f"{task.code} 应默认启用")


class BackupMirrorTests(TestCase):
    """审查 C2：异地副本复制——成功目录写入、失败目录只记警告。"""

    def test_mirror_copies_and_reports_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg = root / f"backup_20260101_020000{bk.ENCRYPTED_SUFFIX}"
            pkg.write_bytes(b"package-bytes")
            good = root / "mirror-a"
            bad = Path("Z:/definitely/not/a/drive/workrecord")

            from django.test import override_settings

            with override_settings(BACKUP_MIRROR_DIRS=[str(good), str(bad)]):
                results = bk._mirror_backups(pkg)

            self.assertEqual(len(results), 2)
            self.assertTrue(results[0]["ok"])
            self.assertTrue((good / pkg.name).is_file())
            self.assertEqual((good / pkg.name).read_bytes(), b"package-bytes")
            self.assertFalse(results[1]["ok"])
            self.assertIn("error", results[1])


class BackupRowPathAttrTests(TestCase):
    """页面渲染的 data-path 必须与真实路径逐字节一致。

    回归来源：模板里错用 ``|escapejs``（JS 转义）导致 Windows 路径的反斜杠被翻倍，
    点「恢复」时报「备份包路径无效」——服务端渲染的行必错，扫描后 JS 渲染的行却正常，
    所以表现为「有时能恢复、刷新后又不行」。
    """

    @classmethod
    def setUpTestData(cls):
        call_command("sync_rbac", verbosity=0)
        cls.root = User.objects.create_superuser("path_root", "p@e.com", PWD)

    def test_data_path_matches_real_path(self):
        import re

        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / f"backup_20260101_020000{bk.ENCRYPTED_SUFFIX}"
            pkg.write_bytes(b"x")

            from unittest import mock

            self.client.force_login(self.root)
            with mock.patch.object(
                sysbackup_views.services, "scan_backups",
                return_value=([{
                    "name": pkg.name,
                    "path": str(pkg),
                    "size": 1,
                    "size_text": "1 B",
                    "mtime": "2026-01-01 02:00:00",
                    "created_at": "2026-01-01 02:00:00",
                    "created_by": "测试",
                    "django_version": "",
                    "db_tables": 0,
                    "db_rows": 0,
                    "media_files": 0,
                    "schema_version": 1,
                    "encrypted": True,
                }], tmp),
            ):
                html = self.client.get("/system/backup/").content.decode()

        m = re.search(r'data-path="([^"]*)"', html)
        self.assertIsNotNone(m, "页面没有渲染 data-path")
        self.assertEqual(m.group(1), str(pkg), "data-path 必须与真实路径一致（反斜杠不得翻倍）")
        self.assertNotIn("\\\\", m.group(1))


class BackupKeyResilienceTests(TestCase):
    """密钥相关加固：多密钥解密 + 密钥无法持久化时必须中止加密。

    事故背景（2026-10-25）：端口上残留了旧版本服务进程，旧代码把密钥重新生成在
    备份目录里却没落盘成功，导致两个备份包从此无法解密。以下两条断言分别锁住
    「老包还能解开」和「不再产生打不开的新包」。
    """

    def test_decrypt_tries_all_known_keys(self):
        from cryptography.fernet import Fernet
        from unittest import mock

        primary = Fernet.generate_key().decode()
        historical = Fernet.generate_key().decode()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary_file = root / "primary" / ".backup_key"
            primary_file.parent.mkdir(parents=True)
            primary_file.write_text(primary, encoding="utf-8")
            legacy_file = root / "backups" / ".backup_key"
            legacy_file.parent.mkdir(parents=True)
            legacy_file.write_text(historical, encoding="utf-8")

            # 用历史密钥加密一个包
            payload = b"payload-encrypted-with-historical-key"
            pkg = root / f"backup_x{bk.ENCRYPTED_SUFFIX}"
            pkg.write_bytes(Fernet(historical.encode()).encrypt(payload))

            out = root / "out.zip"
            with mock.patch.object(bk, "BACKUP_KEY_FILE", primary_file), \
                    mock.patch.object(bk, "_LEGACY_BACKUP_KEY_FILES", [legacy_file]):
                bk._decrypt_file(pkg, out)   # 不应该抛异常
            self.assertEqual(out.read_bytes(), payload)

    def test_encrypt_aborts_when_key_cannot_persist(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key_file = root / "keys" / ".backup_key"

            with mock.patch.object(bk, "BACKUP_KEY_FILE", key_file), \
                    mock.patch.object(bk, "_LEGACY_BACKUP_KEY_FILES", []), \
                    mock.patch("pathlib.Path.write_text", side_effect=OSError("拒绝访问")):
                with self.assertRaises(RuntimeError):
                    bk._fernet()


class UnreadablePackageVisibilityTests(TestCase):
    """无法解密的包必须出现在列表里并标记，而不是静默消失。

    回归来源：旧代码 `_read_backup_summary` 出错时 `return None`，导致密钥缺失/
    损坏的包在页面上「凭空消失」，用户只会看到备份数量变少却不知道为什么。
    """

    def test_unreadable_package_listed_with_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            good = dest / f"backup_20260101_010101{bk.ENCRYPTED_SUFFIX}"
            good.write_bytes(b"not-a-valid-fernet-token")   # 无法解密
            readable = dest / "backup_20260101_020202.zip"
            import zipfile

            with zipfile.ZipFile(readable, "w") as zf:
                zf.writestr("meta.json", '{"schema_version": 1, "created_at": "2026-01-01T02:02:02",'
                                         ' "created_by": "test", "django_version": "5.2",'
                                         ' "database": {"tables": 1, "rows": 1},'
                                         ' "media": {"file_count": 0, "files": []}}')
                zf.writestr("db.sqlite3", b"")

            with self.settings(BACKUP_ALLOWED_ROOTS=[str(dest)]):
                items, _dir = bk.scan_backups(str(dest))

            by_name = {i["name"]: i for i in items}
            self.assertIn(good.name, by_name, "坏包不能从列表中消失")
            self.assertFalse(by_name[good.name]["readable"])
            self.assertTrue(by_name[readable.name]["readable"])
