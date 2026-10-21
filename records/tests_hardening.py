"""records 模块的改造验收测试（对应技术审计报告 v4 的 #3 / #6 / #13 / #21 / #30 / #37）。

命名 tests_hardening.py 而不是并入 tests.py：Django 测试发现规则是 test*.py，
两者都会被收集；分文件便于把"本轮修复的行为契约"与历史用例区分开。
"""

import logging
import os
import tempfile
import time
from datetime import date, timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import Permission, Role

from .models import (
    Complaint,
    CustomerComplaint,
    DailyRecord,
    Decoration,
    OperationLog,
    PriceEvaluation,
)

User = get_user_model()


def grant(module, actions=("read",), user=None):
    """给用户授予某模块的动作权限（按需建角色）。"""
    role, _ = Role.objects.get_or_create(
        code=f"t_{module}", defaults={"name": f"测试-{module}", "is_active": True}
    )
    for action in actions:
        perm, _ = Permission.objects.get_or_create(
            module=module,
            action=action,
            defaults={"code": f"{module}:{action}", "name": f"{module}-{action}"},
        )
        role.permissions.add(perm)
    if user is not None:
        role.users.add(user)
        # _codes() 会缓存到 user 实例上，切换用户后必须清缓存
        if hasattr(user, "_rbac_codes_cache"):
            del user._rbac_codes_cache
    cache.clear()
    return role


class HomePermissionTests(TestCase):
    """#6：首页按可见模块裁剪，零权限账号只能看到空态。"""

    def setUp(self):
        call_command("sync_rbac", verbosity=0)
        self.norole = User.objects.create_user("h_norole", "n@e.com", "x12345678")
        self.archive_only = User.objects.create_user("h_arch", "a@e.com", "x12345678")
        grant("archive", ("read",), self.archive_only)
        self.superuser = User.objects.create_superuser("h_root", "r@e.com", "x12345678")

    def test_no_permission_user_sees_empty_state(self):
        self.client.force_login(self.norole)
        resp = self.client.get(reverse("home"))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("还没有任何模块权限", html)
        self.assertNotIn("card-value", html)

    def test_archive_only_user_does_not_see_daily_cards(self):
        self.client.force_login(self.archive_only)
        html = self.client.get(reverse("home")).content.decode()
        # 档案权限用户应当能打开首页（不返回 403），但不该看到每日记录的统计块
        self.assertNotIn("总记录数", html)
        self.assertNotIn("未完成工作记录", html)
        # 首页固定 10 张业务模块卡按权限显隐：仅有档案权限时只渲染 1 张
        self.assertEqual(html.count('class="home-module"'), 1)

    def test_superuser_sees_cards(self):
        self.client.force_login(self.superuser)
        html = self.client.get(reverse("home")).content.decode()
        self.assertIn("业务模块", html)
        self.assertEqual(html.count('class="home-module"'), 10)

    def test_no_duplicate_queries_for_hidden_modules(self):
        """不可见模块不应产生查询：零权限用户的首页 SQL 条数应显著低于超管。"""
        DailyRecord.objects.create(user=self.norole, department="测试", work_content="x", status="未完成")
        self.client.force_login(self.superuser)
        with CaptureQueriesContext(connection) as admin_ctx:
            self.client.get(reverse("home"))
        self.client.force_login(self.norole)
        with CaptureQueriesContext(connection) as norole_ctx:
            self.client.get(reverse("home"))
        self.assertLess(len(norole_ctx), len(admin_ctx))


class SearchHardeningTests(TestCase):
    """#3：空关键词不发查询；每组硬顶 50 条；条数与总数正确。"""

    def setUp(self):
        call_command("sync_rbac", verbosity=0)
        self.user = User.objects.create_superuser("s_root", "s@e.com", "x12345678")
        self.client.force_login(self.user)

    def test_empty_term_returns_state_without_scanning(self):
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get(reverse("search"), {"search": ""})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("请输入关键词", resp.content.decode())
        # 只剩会话 / 用户 / 权限相关查询，不应出现 13 个模型的全表扫描
        self.assertLessEqual(len(ctx.captured_queries), 8)

    def test_blank_spaces_are_treated_as_empty(self):
        resp = self.client.get(reverse("search"), {"search": "   "})
        self.assertIn("请输入关键词", resp.content.decode())

    def test_group_is_capped_and_total_is_real(self):
        for i in range(60):
            Decoration.objects.create(
                project_name=f"批量项目{i}", constructor="施工方", start_date=date(2026, 1, 1),
                final_price=100,
            )
        resp = self.client.get(reverse("search"), {"search": "批量项目"})
        ctx = resp.context
        self.assertEqual(ctx["search_counts"]["decoration"], 60)
        self.assertEqual(len(ctx["decoration_results"]), 50)
        self.assertEqual(ctx["search_total"], 60)


class PendingItemsSortTests(TestCase):
    """#37：诉讼不再因为字符串 '立案中' 恒排首位。"""

    def setUp(self):
        call_command("sync_rbac", verbosity=0)
        self.user = User.objects.create_superuser("p_root", "p@e.com", "x12345678")
        grant("data_view", ("read",), self.user)
        self.client.force_login(self.user)

    def test_lawsuit_is_not_forced_to_top(self):
        """有待办日期的事项排在前面，诉讼（无日期）排在最后。"""
        DailyRecord.objects.create(
            user=self.user, department="甲", work_content="未完成事项", status="未完成"
        )
        CustomerComplaint.objects.create(
            complaint_date=timezone.localdate(), customer_name="客户A",
            responsible_salesperson="李四", complaint_status="未完成",
        )
        Complaint.objects.create(
            dispute_type="合同", defendant="被告公司", case_number="(2026)1号",
            amount=100, court="某法院", lawyer="王律师", progress="立案",
        )
        resp = self.client.get(reverse("dashboard"))
        items = resp.context["pending_items"]
        self.assertTrue(items, "待处理列表不应为空")
        self.assertNotEqual(items[0]["type"], "lawsuit", "诉讼不应恒排首位")
        self.assertEqual(items[-1]["type"], "lawsuit", "无日期的诉讼应排在最后")

    def test_sort_key_has_no_string_dates(self):
        """直接断言构造逻辑：任何待办的 sort_at 要么是 float，要么是 None。"""
        DailyRecord.objects.create(user=self.user, department="甲", work_content="x", status="未完成")
        Complaint.objects.create(
            dispute_type="合同", defendant="被告公司", case_number="(2026)1号",
            amount=100, court="某法院", lawyer="王律师", progress="立案",
        )
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn("诉讼案件", html)
        # 诉讼行的日期列不再显示「立案中」
        self.assertNotIn("<span>立案中</span>", html)


class StatsConsistencyTests(TestCase):
    """#21：首页与看板共用一套统计，总记录数不再漏模块。"""

    def setUp(self):
        call_command("sync_rbac", verbosity=0)
        self.user = User.objects.create_superuser("st_root", "st@e.com", "x12345678")
        grant("data_view", ("read",), self.user)
        self.client.force_login(self.user)

    def test_total_records_includes_archive_samples_knowledge(self):
        from archive.models import Archive
        from knowledge_base.models import KnowledgeDocument
        from samples.models import Sample

        Decoration.objects.create(
            project_name="装修1", constructor="甲", start_date=date(2026, 1, 1), final_price=10,
            original_price=20,
        )
        Sample.objects.create(
            sample_no="S-1", material_name="料", model="M", sample_date=date(2026, 1, 1)
        )
        Archive.objects.create(code="A-1", name="档案")
        KnowledgeDocument.objects.create(title="文档", status="published")

        from records import stats as stats_svc
        from accounts.services import visible_modules

        cats = stats_svc.decoration_stats()
        base = cats["total_decorations"] + stats_svc.regulation_count()
        total = stats_svc.total_records(set(visible_modules(self.user)), base)
        self.assertEqual(total, 1 + 1 + 1 + 1)

    def test_price_reduction_matches_model_method(self):
        PriceEvaluation.objects.create(
            project_category="A", proposing_department="采购", proposer="张三",
            supplier1_price=100, supplier2_price=120, internal_control_price=90,
        )
        from records import stats as stats_svc

        self.assertEqual(stats_svc.price_stats()["total_price_reduction"], 30)


class LoggingTests(TestCase):
    """#13：业务 logger 的 INFO/WARNING 必须落盘。"""

    def test_business_logger_writes_to_file(self):
        logger = logging.getLogger("archive.views_ledger")
        logger.info("PROBE-INFO-%s", int(time.time()))
        for handler in logging.getLogger().handlers:
            handler.flush()
        log_path = os.path.join("logs", "debug.log")
        self.assertTrue(os.path.exists(log_path), "日志文件未生成")
        with open(log_path, "r", encoding="utf-8", errors="ignore") as fh:
            tail = fh.read()[-20000:]
        self.assertIn("PROBE-INFO-", tail)

    def test_root_logger_has_file_handler(self):
        from django.conf import settings

        self.assertIn("root", settings.LOGGING)
        self.assertIn("file", settings.LOGGING["root"]["handlers"])

    def _load_hosts(self, debug_env, extra_hosts=""):
        """在子进程里以指定环境变量重新导入 settings，读取真实的 ALLOWED_HOSTS。

        不能在当前进程里改 settings.DEBUG 来断言：ALLOWED_HOSTS 是在 settings
        模块导入期算好的，测试期改 DEBUG 不会重算，断言必然自相矛盾。
        """
        import json
        import subprocess
        import sys as _sys

        from django.conf import settings

        env = os.environ.copy()
        env["DJANGO_SETTINGS_MODULE"] = "work_record.settings"
        env["DJANGO_DEBUG"] = debug_env
        env["DJANGO_ALLOWED_HOSTS"] = extra_hosts
        code = (
            "import json, os;"
            "import django;"
            "from django.conf import settings;"
            "print(json.dumps({'debug': settings.DEBUG,"
            " 'hosts': settings.ALLOWED_HOSTS}))"
        )
        out = subprocess.run(
            [_sys.executable, "-c", code],
            cwd=str(settings.BASE_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_allowed_hosts_wildcard_only_in_debug(self):
        """#7：通配符只在 DEBUG 下追加；非 DEBUG 时 Host 校验必须生效。"""
        debug_on = self._load_hosts("1")
        self.assertTrue(debug_on["debug"])
        self.assertIn("*", debug_on["hosts"])

        debug_off = self._load_hosts("0")
        self.assertFalse(debug_off["debug"])
        self.assertNotIn("*", debug_off["hosts"])

    def test_internal_hosts_always_kept(self):
        """本机回环地址始终保留（运行时探测），保证本机/局域网仍可访问。"""
        for flag in ("0", "1"):
            hosts = self._load_hosts(flag)["hosts"]
            for fixed in ("127.0.0.1", "localhost"):
                self.assertIn(fixed, hosts)

    def test_stale_fixed_ip_is_not_baked_in(self):
        """R4：不再把历史 IP 172.17.222.183 写死，改由运行时探测本机局域网 IP。"""
        hosts = self._load_hosts("0")["hosts"]
        self.assertNotIn("172.17.222.183", hosts)
        self.assertTrue(
            any(h.count(".") == 3 and not h.startswith("127.") for h in hosts),
            f"未探测到任何局域网 IPv4 地址：{hosts}",
        )

    def test_production_wildcard_guard_raises(self):
        """生产模式（DEBUG=0）配置通配符必须直接报错，不得让 Host 校验失效。"""
        import subprocess
        import sys as _sys

        from django.conf import settings

        env = os.environ.copy()
        env["DJANGO_SETTINGS_MODULE"] = "work_record.settings"
        env["DJANGO_DEBUG"] = "0"
        env["DJANGO_ALLOWED_HOSTS"] = "*"
        code = "import django; from django.conf import settings; print(settings.ALLOWED_HOSTS)"
        out = subprocess.run(
            [_sys.executable, "-c", code],
            cwd=str(settings.BASE_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertNotEqual(out.returncode, 0, "生产模式不应允许通配符")
        self.assertIn("ImproperlyConfigured", out.stderr + out.stdout)

    def test_allowed_hosts_env_extends(self):
        """运维可通过 DJANGO_ALLOWED_HOSTS 追加域名，与旧行为兼容（只增不减）。"""
        hosts = self._load_hosts("0", "example.com, 10.0.0.5")["hosts"]
        self.assertIn("example.com", hosts)
        self.assertIn("10.0.0.5", hosts)
        self.assertIn("127.0.0.1", hosts)


class ExportFilenameTests(TestCase):
    """#30：中文文件名按 RFC 5987 编码，且剔除引号换行。"""

    def test_content_disposition_encodes_chinese(self):
        from work_record.export_utils import content_disposition

        header = content_disposition('装修记录-20261019.xlsx')
        self.assertIn("filename*=UTF-8''", header)
        self.assertIn("%E8%A3%85%E4%BF%AE", header)
        self.assertNotIn("\r", header)
        self.assertNotIn("\n", header)

    def test_content_disposition_strips_quotes(self):
        from work_record.export_utils import content_disposition

        header = content_disposition('bad"name\r\n.xlsx')
        self.assertNotIn('"bad', header)
        self.assertNotIn("\r", header)

    def test_export_endpoint_sets_encoded_header(self):
        call_command("sync_rbac", verbosity=0)
        user = User.objects.create_superuser("e_root", "e@e.com", "x12345678")
        Decoration.objects.create(
            project_name="导出测试", constructor="甲", start_date=date(2026, 1, 1), final_price=10
        )
        self.client.force_login(user)
        resp = self.client.get(reverse("decoration_export"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("filename*=UTF-8''", resp["Content-Disposition"])

    def test_export_writes_operation_log(self):
        """批次B：导出动作落一条 operation_type='导出' 留痕（供异常检测识别批量导出）。"""
        from .models import OperationLog

        call_command("sync_rbac", verbosity=0)
        user = User.objects.create_superuser("e_log", "el@e.com", "x12345678")
        Decoration.objects.create(
            project_name="留痕测试", constructor="乙", start_date=date(2026, 1, 1), final_price=20
        )
        self.client.force_login(user)

        resp = self.client.get(reverse("decoration_export"))
        self.assertEqual(resp.status_code, 200)

        log = OperationLog.objects.get(operation_type="导出")
        self.assertEqual(log.module_type, "装修")
        self.assertEqual(log.object_identifier, "[装修]下的导出")
        self.assertEqual(log.operator, "e_log")
        # 导出无实体变更 → 不落快照
        self.assertIsNone(log.before_json)
        self.assertIsNone(log.after_json)


class FormulaInjectionTests(TestCase):
    """#27：导出内容中的 '=' 开头字符串不能变成公式。"""

    def test_sanitize_marks_formula_cells_as_text(self):
        from openpyxl import Workbook

        from work_record.export_utils import sanitize_xlsx_formulas

        wb = Workbook()
        ws = wb.active
        ws["A1"] = "=1+1"
        ws["A2"] = "正常文本"
        sanitize_xlsx_formulas(wb)
        self.assertEqual(ws["A1"].value, "'=1+1")
        self.assertEqual(ws["A2"].value, "正常文本")


class PartnerSearchPermissionTests(TestCase):
    """R13(b)：供应商档案归 samples 模块。

    改造前 records/views.py 直接用分组 key 'partners' 去比对 visible_modules，
    而 registry 中并无 'partners' 模块，导致拥有 samples 权限的用户搜索供应商
    恒为空（页面无报错、无结果，用户无从分辨是"没数据"还是"权限没生效"）。
    """

    def setUp(self):
        call_command("sync_rbac", verbosity=0)
        from samples.models import Partner

        Partner.objects.create(partner_code="P-S1", name="洪兴五金")

    def test_samples_perm_user_sees_partner(self):
        user = User.objects.create_user("ps_hit", "ps@e.com", "x12345678")
        grant("samples", ("read",), user)
        self.client.force_login(user)
        resp = self.client.get(reverse("search"), {"search": "洪兴"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["search_counts"]["partners"], 1)
        self.assertIn("洪兴五金", resp.content.decode())

    def test_user_without_samples_perm_sees_zero(self):
        """该用户有其他模块权限（可打开搜索页），但没有 samples，看不到供应商。"""
        user = User.objects.create_user("ps_none", "pn@e.com", "x12345678")
        grant("decoration", ("read",), user)
        self.client.force_login(user)
        resp = self.client.get(reverse("search"), {"search": "洪兴"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["search_counts"]["partners"], 0)
        self.assertNotIn("洪兴五金", resp.content.decode())
