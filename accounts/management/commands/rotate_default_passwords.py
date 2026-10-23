"""一次性口令轮换：把仍在用仓库历史明文口令的账号改为随机强口令。

背景（复核报告 P0-D）：README 曾公开 `X12345678`，而 `rbac_root` / `rbac_admin` /
`rbac_staff` / `rbac_viewer` 四个验收账号在真实库中口令仍为该值——等同凭据泄露。
本命令只改「口令仍等于该历史值」的账号，其它账号与业务数据零改动。

用法：
    python manage.py rotate_default_passwords --dry-run     # 只列出将被轮换的账号
    python manage.py rotate_default_passwords               # 执行并输出随机口令
    python manage.py rotate_default_passwords --out D:\\pwd.txt

新口令只在本命令的 stdout 与 `--out` 文件（默认写到用户主目录下的
WorkRecordBackups/，不落进仓库）中出现一次，请立即转存到密码管理器。
"""

import secrets
import string
from pathlib import Path

from django.utils import timezone

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

# 仓库历史公开过的口令；含该口令的账号一律视为已泄露
LEGACY_DEFAULT_PASSWORD = "X12345678"

# 项目内置验收账号：无论当前口令为何，都应在安全收口时轮换为随机口令
SEED_ACCOUNTS = ("rbac_root", "rbac_admin", "rbac_staff", "rbac_viewer")

# 口令字符集：去掉易混淆的 0/O/1/l/I，长度 16（约 95 bit 熵）
_ALPHABET = "".join(c for c in string.ascii_letters + string.digits if c not in "0O1lI")


def _random_password(length=16):
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


class Command(BaseCommand):
    help = "把仍在使用历史公开口令的账号轮换为随机强口令（一次性安全收口）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true", help="只列出待轮换账号，不修改任何口令"
        )
        parser.add_argument(
            "--out", default="", help="新口令落盘路径（默认用户主目录 WorkRecordBackups/）"
        )
        parser.add_argument(
            "--legacy-password",
            default=LEGACY_DEFAULT_PASSWORD,
            help="判定为已泄露的历史口令（默认 X12345678）",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="额外强制轮换内置验收账号（rbac_root/admin/staff/viewer），不看当前口令",
        )

    def handle(self, *args, **options):
        legacy = options["legacy_password"]
        # 注意：check_password 命中旧轮数哈希时 Django 会顺带回写升级哈希（口令不变）
        targets = [u for u in User.objects.all() if u.check_password(legacy)]
        if options["force"]:
            existing = {u.username for u in targets}
            targets += list(User.objects.filter(username__in=SEED_ACCOUNTS).exclude(
                username__in=existing
            ))
        if not targets:
            self.stdout.write(self.style.SUCCESS("没有账号仍在使用历史公开口令，无需处理。"))
            return

        names = ", ".join(sorted(u.username for u in targets))
        if options["dry_run"]:
            self.stdout.write(f"待轮换账号（{len(targets)}）：{names}")
            return

        results = []
        for user in targets:
            new_password = _random_password()
            user.set_password(new_password)
            user.save(update_fields=["password"])
            results.append((user.username, new_password))

        out_path = self._resolve_out_path(options["out"])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f"# 生成时间：{timezone.localtime():%Y-%m-%d %H:%M:%S}",
            "# 用途：验收账号口令轮换（历史口令已泄露）。请转存密码管理器后删除本文件。",
            "",
        ]
        lines += [f"{name}\t{pwd}" for name, pwd in results]
        out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        try:
            out_path.chmod(0o600)
        except OSError:
            pass

        self.stdout.write(self.style.SUCCESS(f"已轮换 {len(results)} 个账号：{names}"))
        for name, pwd in results:
            self.stdout.write(f"  {name}\t{pwd}")
        self.stdout.write(f"新口令已写入：{out_path}")
        if self._inside_repo(out_path):
            self.stderr.write(
                self.style.WARNING("警告：口令文件位于仓库目录内，提交前务必删除或排除。")
            )

    @staticmethod
    def _resolve_out_path(raw):
        if raw:
            return Path(raw).expanduser().resolve()
        stamp = timezone.localtime().strftime("%Y%m%d_%H%M%S")
        return Path.home() / "WorkRecordBackups" / f"seed_passwords_{stamp}.txt"

    @staticmethod
    def _inside_repo(path):
        try:
            path.resolve().relative_to(Path(settings.BASE_DIR).resolve())
            return True
        except ValueError:
            return False
