"""时区口径守卫（审计 P2-3）。

问题形态：`settings.TIME_ZONE = 'Asia/Shanghai'`、`USE_TZ = True`，但代码里同时
存在两种取"今天"的方式——`timezone.localdate()`（105 处）与 `date.today()`
（14 处）。`date.today()` 取的是**进程所在操作系统的本地日期**：部署到
`TZ=UTC` 的 Linux 容器后，每天北京时间 00:00–08:00 这 8 小时里它返回前一天，
到期判定、"今日新增"、30 天预警窗口整体偏移一天，且只在深夜出现——最容易被
当成"偶发"而长期不修。Windows 本机永远是 Asia/Shanghai，所以本机测不出来。

两件事在这里固化：
1. 业务代码（app 目录）不允许再出现 `date.today(` / `datetime.now(`；
2. `timezone.localdate()` 的口径就是 settings.TIME_ZONE，与进程 TZ 无关。
"""

import os
import re
import time
import unittest
from datetime import UTC, datetime
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

APP_DIRS = [d for d in os.listdir(settings.BASE_DIR) if os.path.isdir(os.path.join(settings.BASE_DIR, d))]
EXEMPT_DIRS = {
    ".venv", ".git", "staticfiles", "htmlcov", "reports", "node_modules",
    "migrations", "scripts", "__pycache__",
}
# 测试模块豁免：它们大量用 date.today() 造"今天"的夹具，属测试自身口径，
# 由各自断言负责；守卫要管的是业务代码（生产路径）。
SELF_NAME = "tests_timezone.py"
PATTERN = re.compile(r"\bdate\.today\(|\bdatetime\.now\(")


def _is_test_module(rel):
    parts = rel.split(os.sep)
    name = parts[-1]
    return name == SELF_NAME or name.startswith("tests") or name.endswith("_tests.py") or "tests" in parts[:-1]


class TimezoneUsageGuardTests(SimpleTestCase):
    def test_no_os_local_date_in_app_code(self):
        """业务代码不允许用进程本地时间取"今天"。

        迁移/测试/脚本/报告目录豁免：迁移是历史快照，测试故意用固定日期，
        脚本与 reports 是一次性运维产物。
        """
        offenders = []
        for top in APP_DIRS:
            for dirpath, dirnames, filenames in os.walk(os.path.join(settings.BASE_DIR, top)):
                dirnames[:] = [d for d in dirnames if d not in EXEMPT_DIRS]
                for name in filenames:
                    if not name.endswith(".py"):
                        continue
                    path = os.path.join(dirpath, name)
                    rel = os.path.relpath(path, settings.BASE_DIR)
                    if _is_test_module(rel) or any(p in EXEMPT_DIRS for p in rel.split(os.sep)):
                        continue
                    with open(path, encoding="utf-8", errors="ignore") as fh:
                        for lineno, line in enumerate(fh, start=1):
                            stripped = line.strip()
                            # 注释行里提到 date.today() 是在解释问题，不是使用
                            if stripped.startswith("#"):
                                continue
                            if PATTERN.search(line):
                                offenders.append(f"{rel}:{lineno}: {stripped}")
        self.assertEqual(
            offenders, [],
            "业务代码仍在用进程本地时间（应改 timezone.localdate()/localtime()）：\n"
            + "\n".join(offenders),
        )

    def test_localdate_follows_settings_timezone_not_process_tz(self):
        """localdate 按 settings.TIME_ZONE 计算，与操作系统 TZ 无关。

        固定一个 UTC 20:00 的瞬时：按 UTC 算是 23 日，按 Asia/Shanghai（+8）
        算是 24 日。这正是 date.today() 在容器里错一天的机理。
        """
        instant = datetime(2026, 10, 23, 20, 0, tzinfo=UTC)
        with mock.patch("django.utils.timezone.now", return_value=instant):
            with override_settings(TIME_ZONE="UTC"):
                self.assertEqual(timezone.localdate().isoformat(), "2026-10-23")
            with override_settings(TIME_ZONE="Asia/Shanghai"):
                self.assertEqual(timezone.localdate().isoformat(), "2026-10-24")

    @unittest.skipIf(
        not hasattr(time, "tzset"),
        "Windows 无 time.tzset，无法切换进程时区；本用例在 Linux/macOS 上运行",
    )
    def test_process_tz_does_not_change_localdate(self):
        """即使进程 TZ 被设成 UTC，localdate 仍按 settings.TIME_ZONE（北京）走。"""
        old_tz = os.environ.get("TZ")
        instant = datetime(2026, 10, 23, 20, 0, tzinfo=UTC)
        try:
            os.environ["TZ"] = "UTC"
            time.tzset()
            with mock.patch("django.utils.timezone.now", return_value=instant):
                # date.today() 此时会是 23 日（UTC 口径），而 localdate 应为 24 日
                self.assertEqual(timezone.localdate().isoformat(), "2026-10-24")
        finally:
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz
            time.tzset()
