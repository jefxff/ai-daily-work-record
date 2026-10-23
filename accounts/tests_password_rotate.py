"""验收账号口令轮换命令测试（复核报告 P0-D）。"""

import tempfile
from pathlib import Path

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase

from accounts.management.commands.rotate_default_passwords import SEED_ACCOUNTS

LEGACY = "LegacyPwd123"


class RotateDefaultPasswordsTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "pwd.txt"

    def tearDown(self):
        self.tmp.cleanup()

    def _rotate(self, *extra):
        call_command(
            "rotate_default_passwords",
            "--legacy-password", LEGACY,
            "--out", str(self.out),
            *extra,
        )

    def test_rotate_legacy_password(self):
        u = User.objects.create_user("leaky", password=LEGACY)
        self._rotate()
        u.refresh_from_db()
        self.assertFalse(u.check_password(LEGACY))
        lines = self.out.read_text(encoding="utf-8").strip().splitlines()
        entry = [ln for ln in lines if ln.startswith("leaky\t")][0]
        new_password = entry.split("\t", 1)[1]
        self.assertTrue(u.check_password(new_password))

    def test_other_accounts_untouched(self):
        u = User.objects.create_user("normal", password="StrongPwd#2026")
        before = u.password
        self._rotate()
        u.refresh_from_db()
        self.assertEqual(u.password, before)

    def test_dry_run_changes_nothing(self):
        u = User.objects.create_user("leaky2", password=LEGACY)
        before = u.password
        self._rotate("--dry-run")
        u.refresh_from_db()
        self.assertEqual(u.password, before)
        self.assertFalse(self.out.exists())

    def test_force_rotates_seed_accounts(self):
        name = SEED_ACCOUNTS[0]
        u = User.objects.create_user(name, password="NotTheLegacyOne1")
        self._rotate("--force")
        u.refresh_from_db()
        self.assertFalse(u.check_password("NotTheLegacyOne1"))
