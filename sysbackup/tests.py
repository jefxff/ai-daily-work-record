"""sysbackup 最小回归测试：路径白名单拦截 + 备份包校验失败不落盘（H4）。

sysbackup 含「整库恢复」这类最高危写操作，此前无任何测试。本文件覆盖
两条关键防线：路径白名单（禁止访问项目外任意路径）、恢复前的备份包结构校验
（校验不通过必须直接失败，绝不能改写数据库内容）。
"""

import json
import sqlite3
import tempfile
import uuid
import zipfile
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import Group
from django.db import connection
from django.test import TestCase, TransactionTestCase

from . import services


class PathWhitelistTests(TestCase):
    """备份中心所有路径操作必须限制在白名单目录内。"""

    def test_path_inside_base_dir_is_allowed(self):
        services.ensure_path_allowed(Path(settings.BASE_DIR))

    def test_path_outside_whitelist_is_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            with self.assertRaises(ValueError):
                services.ensure_path_allowed(Path(outside))

    def test_delete_backup_rejects_path_outside_whitelist(self):
        with tempfile.TemporaryDirectory() as outside:
            fake = Path(outside) / "x.zip"
            fake.write_bytes(b"PK")  # 真实存在且为 .zip，但不在白名单内
            with self.assertRaises(ValueError):
                services.delete_backup(fake)

    def test_delete_backup_rejects_non_absolute_or_non_zip(self):
        with self.assertRaises(ValueError):
            services.delete_backup(Path("relative.zip"))

    def test_start_restore_rejects_non_zip(self):
        with self.assertRaises(ValueError):
            services.start_restore("not-a-zip.txt")


class BackupPackageValidationTests(TestCase):
    """备份包结构校验：任何一项不达标都必须被 _inspect_backup 拦截。"""

    def _make_package(self, directory, meta):
        path = Path(directory) / "pkg.zip"
        with zipfile.ZipFile(path, "w") as zf:
            if meta is not None:
                zf.writestr(services.META_ENTRY, json.dumps(meta))
            zf.writestr(services.DB_ENTRY, b"")
        return path

    def test_missing_meta_is_reported(self):
        with tempfile.TemporaryDirectory() as td:
            _, errors = services._inspect_backup(self._make_package(td, None))
        self.assertTrue(any("meta.json" in e for e in errors))

    def test_schema_version_mismatch_is_reported(self):
        meta = {"schema_version": services.SCHEMA_VERSION + 1,
                "django_version": "4.2", "media": {"files": []}}
        with tempfile.TemporaryDirectory() as td:
            _, errors = services._inspect_backup(self._make_package(td, meta))
        self.assertTrue(any("结构版本" in e for e in errors))

    def test_django_major_mismatch_is_reported(self):
        meta = {"schema_version": services.SCHEMA_VERSION,
                "django_version": "3.2", "media": {"files": []}}
        with tempfile.TemporaryDirectory() as td:
            _, errors = services._inspect_backup(self._make_package(td, meta))
        self.assertTrue(any("Django 主版本" in e for e in errors))

    def test_missing_attachment_is_reported(self):
        meta = {"schema_version": services.SCHEMA_VERSION,
                "django_version": services._django_version(),
                "media": {"files": ["lost.bin"]}}
        with tempfile.TemporaryDirectory() as td:
            _, errors = services._inspect_backup(self._make_package(td, meta))
        self.assertTrue(any("附件清单缺失" in e for e in errors))

    def test_run_restore_with_invalid_package_marks_failed_without_touching_data(self):
        """恢复包校验不通过：任务标记失败，且业务数据（此处以 auth.Group 为哨兵）零改动。"""
        sentinel = Group.objects.create(name="恢复校验哨兵")
        before = Group.objects.count()

        meta = {"schema_version": services.SCHEMA_VERSION + 1,
                "django_version": "4.2", "media": {"files": []}}
        with tempfile.TemporaryDirectory() as td:
            package = self._make_package(td, meta)
            task_id = services._new_task("restore")
            # 把 JSONL 留痕文件重定向到临时目录，避免污染真实 backups/ 目录
            with mock.patch.object(services, "OPLOG_FILE", Path(td) / "oplogs.jsonl"):
                services._run_restore(task_id, package, "tester")

            snapshot = services._task_snapshot(task_id)
            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot["status"], "failed")
            self.assertIn("校验未通过", snapshot.get("error", ""))

        self.assertTrue(Group.objects.filter(pk=sentinel.pk).exists())
        self.assertEqual(Group.objects.count(), before)


class BackupEncryptionTests(TestCase):
    """审计 P2-4：备份包必须加密落盘，且旧明文包仍可识别/恢复。"""

    def test_default_backup_key_is_outside_project_tree(self):
        """审计 SEC-04：默认密钥文件不得落在 BASE_DIR 内。"""
        default = Path(settings.BASE_DIR) / ".backup_key"
        if services.BACKUP_KEY_FILE == default:
            self.skipTest("测试环境显式用 BACKUP_KEY_FILE 指回了项目内路径")
        self.assertFalse(
            str(services.BACKUP_KEY_FILE).lower().startswith(str(settings.BASE_DIR).lower())
        )

    def test_encrypt_decrypt_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            key_file = Path(d) / "backup.key"
            src = Path(d) / "package.zip"
            enc = Path(d) / "package.zip.enc"
            out = Path(d) / "restored.zip"
            src.write_bytes(b"PK\x03\x04secret-database")
            with mock.patch.object(services, "BACKUP_KEY_FILE", key_file):
                services._encrypt_file(src, enc)
                self.assertNotEqual(enc.read_bytes()[:4], b"PK\x03\x04")
                services._decrypt_file(enc, out)
            self.assertEqual(out.read_bytes(), src.read_bytes())

    def test_scan_summary_reads_encrypted_package(self):
        with tempfile.TemporaryDirectory() as d:
            key_file = Path(d) / "backup.key"
            plain = Path(d) / "backup_20260101_000000.zip"
            enc = Path(d) / "backup_20260101_000000.zip.enc"
            meta = {
                "schema_version": 1,
                "django_version": "4.2.0",
                "database": {"tables": 1, "rows": 2},
                "media": {"file_count": 0, "files": []},
                "created_at": "2026-01-01T00:00:00",
            }
            with zipfile.ZipFile(plain, "w") as zf:
                zf.writestr(services.META_ENTRY, json.dumps(meta))
            with mock.patch.object(services, "BACKUP_KEY_FILE", key_file):
                services._encrypt_file(plain, enc)
                info = services._read_backup_summary(enc)
            self.assertIsNotNone(info)
            self.assertTrue(info["encrypted"])
            self.assertEqual(info["db_tables"], 1)

    def test_start_restore_accepts_encrypted_suffix(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "backup_20260101_000000.zip.enc"
            p.write_bytes(b"not-a-real-package")
            # 只验证后缀白名单放行；实际恢复线程由 _run_restore 校验并失败
            with mock.patch.object(services, "ensure_path_allowed"), \
                    mock.patch.object(services.threading, "Thread"):
                task_id = services.start_restore(str(p))
            self.assertTrue(task_id)


class ZipSlipPathFenceTests(TestCase):
    """SEC-H1 回归：备份恢复 Zip Slip 路径穿越防护。"""

    def test_safe_rel_rejects_parent_dir_traversal(self):
        # ../ 穿越
        self.assertIsNone(services._safe_rel("../../../etc/passwd"))
        self.assertIsNone(services._safe_rel("subdir/../../../tmp/x"))

    def test_safe_rel_rejects_absolute_path(self):
        self.assertIsNone(services._safe_rel("/etc/passwd"))
        self.assertIsNone(services._safe_rel("\\Windows\\System32\\evil"))

    def test_safe_rel_rejects_anti_slash_traversal(self):
        # 反斜杠穿越：攻击者可能用 Windows 分隔符绕过
        self.assertIsNone(services._safe_rel("..\\..\\..\\etc\\passwd"))
        self.assertIsNone(services._safe_rel("media\\..\\..\\templates\\x.html"))

    def test_safe_rel_rejects_empty_and_dot_only(self):
        self.assertIsNone(services._safe_rel(""))
        self.assertIsNone(services._safe_rel("."))
        self.assertIsNone(services._safe_rel("././"))

    def test_safe_rel_passes_normal_paths(self):
        self.assertEqual(services._safe_rel("proofs/a/b.jpg"), "proofs/a/b.jpg")
        self.assertEqual(services._safe_rel("proofs/./a/../b.jpg"), "proofs/b.jpg")

    def test_inspect_backup_rejects_package_with_traversal_entries(self):
        meta = {"schema_version": services.SCHEMA_VERSION,
                "django_version": services._django_version(),
                "media": {"files": ["../../etc/passwd"]}}
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "evil.zip"
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr(services.META_ENTRY, json.dumps(meta))
                zf.writestr(services.DB_ENTRY, b"")
                # 恶意条目：看起来以 media/ 开头，但实际 ../ 穿越
                zf.writestr("media/../../../etc/passwd", b"malicious content")
            _, errors = services._inspect_backup(path)
        self.assertTrue(any("可疑条目" in e or "路径穿越" in e for e in errors),
                        f"未捕获到 Zip Slip，实际 errors={errors}")

    def test_write_storage_file_raises_on_traversal_rel(self):
        from django.core.exceptions import SuspiciousOperation
        with self.assertRaises(SuspiciousOperation):
            services._write_storage_file("../../../etc/passwd", None)


class BackupRestoreRoundTripTests(TransactionTestCase):
    """OBS-01：备份可用性回环——真实产包 → 结构校验 → 落到临时库确认数据可还原。

    刻意不在测试里调用 _run_restore 覆盖当前测试库：TestCase 事务隔离下，
    覆盖中的 SQLite 文件会让 Django 连接读到半成品状态（历史上曾因此丢数据）。
    这里改用与恢复完全相同的 sqlite3 online backup API 把包内 db 导入临时文件，
    等价验证「这份备份包里的数据能还原」，同时对测试库零副作用。
    """

    def setUp(self):
        self.dest = Path(settings.BASE_DIR) / "backups" / "test_roundtrip"
        self.dest.mkdir(parents=True, exist_ok=True)
        self.marker = f"roundtrip-marker-{uuid.uuid4().hex[:8]}"
        Group.objects.create(name=self.marker)

    def tearDown(self):
        Group.objects.filter(name=self.marker).delete()
        for p in self.dest.glob("*"):
            try:
                p.unlink()
            except OSError:
                pass
        try:
            self.dest.rmdir()
        except OSError:
            pass

    def test_backup_package_can_be_restored(self):
        # Django 的 SQLite 测试库默认是 :memory:，备份线程另开连接只会看到空库。
        # 这里先用 online backup API 把内存库落成真实文件，再把备份指向该文件。
        live = self.dest / "live.sqlite3"
        dst = sqlite3.connect(str(live))
        connection.connection.backup(dst)
        dst.close()

        key_file = self.dest / "backup.key"
        patched_db = {**settings.DATABASES["default"], "NAME": str(live)}
        with mock.patch.dict(settings.DATABASES, {"default": patched_db}), \
                mock.patch.object(services, "BACKUP_KEY_FILE", key_file):
            task_id = services.start_backup(dest_dir=str(self.dest), operator="test")
            snap = services.wait_task(task_id, timeout=180)
            self.assertEqual(snap["status"], "success", snap.get("error"))

            pkg = Path(snap["result"]["path"])
            self.assertTrue(pkg.exists())
            self.assertTrue(pkg.name.endswith(services.ENCRYPTED_SUFFIX))
            self.assertNotEqual(pkg.read_bytes()[:4], b"PK\x03\x04", "备份包必须加密落盘")

            plain = self.dest / "unpacked.zip"
            services._decrypt_file(pkg, plain)

        meta, errors = services._inspect_backup(plain)
        self.assertEqual(errors, [])
        self.assertGreater(meta["database"]["tables"], 0)
        self.assertGreater(meta["database"]["rows"], 0)

        with zipfile.ZipFile(plain) as zf:
            zf.extract(services.DB_ENTRY, self.dest)
        restored_db = self.dest / "restored.sqlite3"
        src = sqlite3.connect(str(self.dest / services.DB_ENTRY))
        dst = sqlite3.connect(str(restored_db))
        with dst:
            src.backup(dst)
        src.close()
        dst.close()

        with sqlite3.connect(str(restored_db)) as conn:
            names = [row[0] for row in conn.execute("SELECT name FROM auth_group")]
        self.assertIn(self.marker, names, "备份包内数据库缺少标记数据，备份不可用")
