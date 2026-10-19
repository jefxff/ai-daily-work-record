# -*- coding: utf-8 -*-
"""RBAC 登录认证与权限系统验收测试（对应 spec.md AC-1 ~ AC-12）。

运行：python manage.py test accounts -v 1
每个测试方法的 docstring 标注其覆盖的 AC 编号。
"""

from django.core.cache import cache
from django.core.management import call_command
from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.urls import reverse

from rest_framework.test import APIClient

from .models import LoginLog, Permission, Role
from .registry import MODULE_KEYS, permission_code
from .services import has_perm, visible_modules
from records.models import Insurance

PWD = "X12345678"
PWD2 = "Y12345678y"


class RbacAuthTests(TestCase):
    """AC-1~AC-12 全量验收。"""

    @classmethod
    def setUpTestData(cls):
        call_command("sync_rbac", verbosity=0)
        cls.role_viewer = Role.objects.get(code="viewer")
        cls.role_staff = Role.objects.get(code="staff")
        cls.role_admin = Role.objects.get(code="admin")

        cls.superuser = User.objects.create_superuser("root_ac", "root@e.com", PWD)
        cls.admin = User.objects.create_user("admin_ac", "admin@e.com", PWD, is_active=True)
        cls.admin.rbac_roles.add(cls.role_admin)
        cls.viewer = User.objects.create_user("viewer_ac", "v@e.com", PWD, is_active=True)
        cls.viewer.rbac_roles.add(cls.role_viewer)
        cls.staff = User.objects.create_user("staff_ac", "s@e.com", PWD, is_active=True)
        cls.staff.rbac_roles.add(cls.role_staff)
        cls.norole = User.objects.create_user("norole_ac", "n@e.com", PWD, is_active=True)

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    # ---------- 辅助 ----------
    def login(self, user, klass=Client):
        c = klass()
        c.force_login(user)
        return c

    def post_login(self, username, password, client=None):
        c = client or Client()
        return c.post(reverse("accounts:login"), {"username": username, "password": password})

    # ---------- AC-1 ----------
    def test_ac01_anonymous_blocked(self):
        """AC-1: 未登录访问全站页面 302 带 next，DRF 401，登录/注册页可匿名访问。"""
        c = Client()
        page_paths = [
            reverse("home"),
            reverse("daily_record_list"),
            reverse("knowledge_base:document_list"),
            reverse("samples:list"),
            reverse("accounts:user_list"),
        ]
        for path in page_paths:
            r = c.get(path)
            self.assertEqual(r.status_code, 302, path)
            self.assertIn("/accounts/login/", r["Location"], path)
            self.assertIn("next=", r["Location"], path)
        # DRF 接口匿名 401 JSON
        r = c.get("/api/daily-record/")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r["Content-Type"], "application/json")
        # 白名单页面匿名可访问
        self.assertEqual(c.get(reverse("accounts:login")).status_code, 200)
        self.assertEqual(c.get(reverse("accounts:register")).status_code, 200)

    # ---------- AC-2 ----------
    def test_ac02_register_review_flow(self):
        """AC-2: 注册后 is_active=False 待审核；审核前无法登录，审核后可登录且按角色显菜单。"""
        c = Client()
        r = c.post(reverse("accounts:register"), {
            "username": "newbie_ac", "password": PWD, "confirm": PWD,
            "real_name": "新人甲", "department": "测试部",
        })
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.context["submitted"])
        newbie = User.objects.get(username="newbie_ac")
        self.assertFalse(newbie.is_active)
        self.assertTrue(LoginLog.objects.filter(username="newbie_ac", login_type="register").exists())

        # 待审核登录被拒（留在登录页，不跳转）
        r = self.post_login("newbie_ac", PWD)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(LoginLog.objects.filter(username="newbie_ac", success=False).exists())

        # 管理员审核通过并分配 viewer
        ac = self.login(self.admin)
        r = ac.post(reverse("accounts:user_approve", args=[newbie.pk]))
        self.assertEqual(r.status_code, 302)
        newbie.rbac_roles.add(self.role_viewer)
        newbie.refresh_from_db()
        self.assertTrue(newbie.is_active)

        r = self.post_login("newbie_ac", PWD)
        self.assertEqual(r.status_code, 302)
        self.assertTrue(LoginLog.objects.filter(username="newbie_ac", success=True).exists())

        # viewer 侧边栏不含系统管理
        c2 = Client()
        c2.force_login(newbie)
        html = c2.get(reverse("home")).content.decode()
        self.assertNotIn("用户管理", html)

    # ---------- AC-3 ----------
    def test_ac03_login_lockout(self):
        """AC-3: 连续 5 次错误密码后锁 15 分钟，期间正确密码同样被拒；清锁后可登录。"""
        for _ in range(5):
            self.post_login("staff_ac", "wrong-pass-9")
        # 第 6 次正确密码仍被拒
        r = self.post_login("staff_ac", PWD)
        self.assertEqual(r.status_code, 200)
        # 清除锁定（模拟 15 分钟到期/管理员解锁）
        cache.clear()
        r = self.post_login("staff_ac", PWD)
        self.assertEqual(r.status_code, 302)

    # ---------- AC-4 ----------
    def test_ac04_rbac_matrix(self):
        """AC-4: staff 业务模块 C/R/U 真、D/system 假；viewer 仅 read；双角色并集；无角色全假；超管全真。"""
        business = ["daily_record", "decoration", "customer_credit", "customer_complaint",
                    "complaint", "regulation", "price_evaluation", "insurance"]
        for m in business:
            for a in ("create", "read", "update"):
                self.assertTrue(has_perm(self.staff, m, a), f"staff {m}.{a}")
            self.assertFalse(has_perm(self.staff, m, "delete"), f"staff {m}.delete")
        for a in ("create", "read", "update", "delete"):
            self.assertFalse(has_perm(self.staff, "system", a))
            self.assertTrue(has_perm(self.viewer, business[0], "read") if a == "read"
                            else not has_perm(self.viewer, business[0], a))
        self.assertTrue(has_perm(self.viewer, "samples", "read"))
        self.assertFalse(has_perm(self.viewer, "samples", "create"))

        # 双角色并集：staff + 仅 samples.delete 的自定义角色
        role = Role.objects.create(code="del_samples", name="试样删除员", is_active=True)
        role.permissions.add(Permission.objects.get(code=permission_code("samples", "delete")))
        dual = User.objects.create_user("dual_ac", "d@e.com", PWD, is_active=True)
        dual.rbac_roles.add(self.role_staff, role)
        self.assertTrue(has_perm(dual, "samples", "delete"))
        self.assertTrue(has_perm(dual, "samples", "create"))
        self.assertFalse(has_perm(dual, "knowledge", "delete"))

        # 无角色用户全假
        for m in ["daily_record", "samples", "system"]:
            for a in ("create", "read", "update", "delete"):
                self.assertFalse(has_perm(self.norole, m, a), f"norole {m}.{a}")

        # 超管全真
        for m in ["system", "samples", "metal_price"]:
            for a in ("create", "read", "update", "delete"):
                self.assertTrue(has_perm(self.superuser, m, a))

        # visible_modules 与判定一致
        vis = set(visible_modules(self.staff))
        for m in MODULE_KEYS:
            expected = any(has_perm(self.staff, m, a) for a in ("create", "read", "update", "delete"))
            self.assertEqual(m in vis, expected, m)
        self.assertEqual(set(visible_modules(self.norole)), set())

    # ---------- AC-5 ----------
    def test_ac05_view_enforcement(self):
        """AC-5: viewer 直接 POST 写 URL 一律 403 且库行数不变；无角色访问业务页 403。"""
        cv = self.login(self.viewer)
        from records.models import Insurance
        from knowledge_base.models import KnowledgeDocument
        n_ins, n_doc = Insurance.objects.count(), KnowledgeDocument.objects.count()
        # 写操作（GET 表单页同样拒绝）：两种模块
        self.assertEqual(cv.get(reverse("insurance_add")).status_code, 403)
        self.assertEqual(cv.post(reverse("insurance_delete", args=[999999])).status_code, 403)
        self.assertEqual(cv.get(reverse("knowledge_base:document_create")).status_code, 403)
        self.assertEqual(cv.post(reverse("knowledge_base:document_delete", args=[999999])).status_code, 403)
        self.assertEqual(Insurance.objects.count(), n_ins)
        self.assertEqual(KnowledgeDocument.objects.count(), n_doc)
        # 有权限的读页面 200
        self.assertEqual(cv.get(reverse("insurance_list")).status_code, 200)
        self.assertEqual(cv.get(reverse("knowledge_base:category_list")).status_code, 200)

        # 无角色用户访问任意业务模块 403
        cn = self.login(self.norole)
        self.assertEqual(cn.get(reverse("daily_record_list")).status_code, 403)
        self.assertEqual(cn.get(reverse("samples:list")).status_code, 403)
        self.assertEqual(cn.get(reverse("knowledge_base:document_list")).status_code, 403)

    # ---------- AC-6 ----------
    def test_ac06_drf_permission(self):
        """AC-6: DRF 矩阵：匿名 401 / viewer 200+403 / staff 201+403。"""
        payload = {
            "company_name": "AC6保险", "title": "AC6保单", "premium": "88.00",
            "issue_date": "2026-01-01", "end_date": "2026-12-31", "department": "质量部",
        }
        anon = APIClient()
        self.assertEqual(anon.get("/api/insurance/").status_code, 401)
        self.assertEqual(anon.post("/api/insurance/", payload, format="json").status_code, 401)

        cv = APIClient(); cv.force_login(self.viewer)
        self.assertEqual(cv.get("/api/insurance/").status_code, 200)
        self.assertEqual(cv.post("/api/insurance/", payload, format="json").status_code, 403)
        self.assertEqual(cv.delete("/api/insurance/999999/").status_code, 403)

        cs = APIClient(); cs.force_login(self.staff)
        self.assertEqual(cs.get("/api/insurance/").status_code, 200)
        r = cs.post("/api/insurance/", payload, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(cs.delete(f"/api/insurance/{r.json()['id']}/").status_code, 403)

        # AI 函数视图同样受模块权限约束（评审 Finding-1 收口）
        ai_payload = {"model": "insurance", "data": payload}
        # viewer 有 read 无 create：AI 创建 403，行数不增
        before = Insurance.objects.count()
        self.assertEqual(cv.post("/api/ai/create/", ai_payload, format="json").status_code, 403)
        self.assertEqual(Insurance.objects.count(), before)
        # staff 有 create：AI 通道可创建
        self.assertEqual(cs.post("/api/ai/create/", ai_payload, format="json").status_code, 201)
        # 无角色用户：模型清单为空、全局搜索为空、总结无业务数据、问答拒绝且不带数据
        cn = APIClient(); cn.force_login(self.norole)
        self.assertEqual(cn.get("/api/ai/models/").json(), {})
        self.assertEqual(cn.get("/api/ai/search/?q=公司").json(), {})
        self.assertEqual(cn.get("/api/ai/search/?q=公司&model=insurance").status_code, 403)
        sj = cn.get("/api/ai/summary/").json()
        self.assertEqual(sj["总数据统计"], {})
        self.assertEqual(sj["今日数据"], {})
        qj = cn.get("/api/ai/query/?question=保险有哪些").json()
        self.assertIn("无权", qj["答案"])
        self.assertIsNone(qj["数据"])
        # viewer（有 insurance read）：清单含 insurance、问答可返回数据
        self.assertIn("insurance", cv.get("/api/ai/models/").json())
        self.assertEqual(cv.get("/api/ai/query/?question=保险有哪些").status_code, 200)

    # ---------- AC-7 ----------
    def test_ac07_menu_and_buttons(self):
        """AC-7: viewer 看不到写按钮和系统组；staff 看得到新增但看不到删除。"""
        cv = self.login(self.viewer)
        html = cv.get(reverse("samples:list")).content.decode()
        self.assertNotIn(reverse("samples:create"), html)
        self.assertNotIn("送样登记", html)
        self.assertNotIn("用户管理", html)
        # 导出（read）保留
        self.assertIn("导出", html)

        cs = self.login(self.staff)
        html = cs.get(reverse("samples:list")).content.decode()
        self.assertIn(reverse("samples:create"), html)
        # staff 无 delete：列表中不应出现样品删除链接
        import re
        self.assertIsNone(re.search(r"/samples/\d+/delete/", html))

    # ---------- AC-8 ----------
    def test_ac08_custom_role_matrix(self):
        """AC-8: 管理员界面新建角色（仅 samples C/R/U），分配后只可见 samples，删除 403，内置角色不可删。"""
        ac = self.login(self.admin)
        perms = [permission_code("samples", a) for a in ("create", "read", "update")]
        r = ac.post(reverse("accounts:role_create"), {
            "code": "sample_specialist", "name": "试样专员",
            "description": "仅试样模块", "is_active": "on", "perms": perms,
        })
        self.assertEqual(r.status_code, 302)
        role = Role.objects.get(code="sample_specialist")
        self.assertEqual(set(role.permissions.values_list("code", flat=True)), set(perms))

        user_a = User.objects.create_user("spec_ac", "sp@e.com", PWD, is_active=True)
        user_a.rbac_roles.add(role)
        ca = self.login(user_a)
        self.assertEqual(ca.get(reverse("samples:list")).status_code, 200)
        self.assertEqual(ca.get(reverse("daily_record_list")).status_code, 403)
        self.assertEqual(ca.post(reverse("samples:delete", args=[999999])).status_code, 403)

        # 内置角色不允许删除：删除被拒绝且角色仍在
        r = ac.post(reverse("accounts:role_delete", args=[self.role_viewer.pk]))
        self.assertTrue(Role.objects.filter(pk=self.role_viewer.pk).exists())

    # ---------- 内置角色权限放开 ----------
    def test_builtin_role_perm_edit_and_sync_kept(self):
        """内置角色可在界面调整权限矩阵；sync_rbac 默认不重置，--reset-builtins 才强制对齐。"""
        from .rbac_init import builtin_role_codes

        ac = self.login(self.admin)
        perms = [permission_code("archive", a) for a in ("create", "read", "update")]
        r = ac.post(reverse("accounts:role_edit", args=[self.role_staff.pk]), {
            "code": "staff", "name": "普通员工", "description": "试图改名", "perms": perms,
        })
        self.assertEqual(r.status_code, 302)
        self.role_staff.refresh_from_db()
        self.assertEqual(set(self.role_staff.permissions.values_list("code", flat=True)), set(perms))
        # 内置角色基础信息仍锁定：名称/说明不受界面提交影响
        self.assertEqual(self.role_staff.name, "普通员工")
        self.assertEqual(
            self.role_staff.description,
            "全部业务模块的读取、新增与编辑权限（无删除、无系统管理）",
        )

        # staff 获得档案读取后可见档案侧边栏入口
        cs = self.login(self.staff)
        self.assertEqual(cs.get("/archive/").status_code, 200)

        # sync_rbac 默认不覆盖界面调整
        call_command("sync_rbac", verbosity=0)
        self.role_staff.refresh_from_db()
        self.assertEqual(set(self.role_staff.permissions.values_list("code", flat=True)), set(perms))

        # --reset-builtins 强制对齐 registry 默认定义
        call_command("sync_rbac", "--reset-builtins", verbosity=0)
        self.role_staff.refresh_from_db()
        self.assertEqual(
            set(self.role_staff.permissions.values_list("code", flat=True)),
            set(builtin_role_codes("staff")),
        )

    # ---------- AC-9 ----------
    def test_ac09_user_admin_and_reset(self):
        """AC-9: 管理员建号/重置密码/停用全闭环；非管理员 403；非超管不能动超管账号。"""
        ac = self.login(self.admin)
        r = ac.post(reverse("accounts:user_create"), {
            "username": "user_b", "password": PWD, "real_name": "B君", "is_active": "on",
            "roles": [str(self.role_viewer.pk)],
        })
        self.assertEqual(r.status_code, 302)
        b = User.objects.get(username="user_b")
        self.assertEqual(self.post_login("user_b", PWD).status_code, 302)

        # 重置密码
        r = ac.post(reverse("accounts:user_reset_password", args=[b.pk]),
                    {"new_password": PWD2, "confirm": PWD2})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.post_login("user_b", PWD).status_code, 200)  # 旧密码失效
        self.assertEqual(self.post_login("user_b", PWD2).status_code, 302)

        # 停用
        r = ac.post(reverse("accounts:user_toggle", args=[b.pk]), {"active": "false"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.post_login("user_b", PWD2).status_code, 200)
        b.refresh_from_db()
        self.assertFalse(b.is_active)

        # 非管理员访问用户管理 403
        cv = self.login(self.viewer)
        self.assertEqual(cv.get(reverse("accounts:user_list")).status_code, 403)

        # 非超管管理员不能停用/改密超管账号
        self.assertEqual(ac.post(reverse("accounts:user_toggle", args=[self.superuser.pk]),
                                 {"active": "false"}).status_code, 403)
        self.assertEqual(ac.get(reverse("accounts:user_reset_password",
                                        args=[self.superuser.pk])).status_code, 403)

    # ---------- AC-10 ----------
    def test_ac10_sessions_and_self_password(self):
        """AC-10: 改密后其它会话立即失效；可主动下线其它会话；会话页展示当前会话标记。"""
        ca, cb = Client(), Client()
        ca.force_login(self.staff)
        cb.force_login(self.staff)
        # 两个会话都可用
        self.assertEqual(ca.get(reverse("samples:list")).status_code, 200)
        self.assertEqual(cb.get(reverse("samples:list")).status_code, 200)

        # A 改密 → B 的会话被密码指纹中间件踢下线
        r = ca.post(reverse("accounts:password"), {
            "old_password": PWD, "new_password1": PWD2, "new_password2": PWD2,
        })
        self.assertEqual(r.status_code, 302)
        r = cb.get(reverse("samples:list"))
        self.assertEqual(r.status_code, 302)
        self.assertIn("/accounts/login/", r["Location"])

        # 主动下线：新密码重新登录两个会话，A 在会话页下线 B
        self.staff.refresh_from_db()  # 刷新密码哈希，避免 force_login 写入旧 HASH_SESSION_KEY
        ca.force_login(self.staff)
        cb.force_login(self.staff)
        b_key = cb.session.session_key
        r = ca.post(reverse("accounts:session_revoke", args=[b_key]))
        self.assertEqual(r.status_code, 302)
        r = cb.get(reverse("samples:list"))
        self.assertEqual(r.status_code, 302)
        # 会话页正常展示且当前会话被标记
        html = ca.get(reverse("accounts:sessions")).content.decode()
        self.assertIn("当前设备", html)
        # 还原密码，避免污染
        self.staff.set_password(PWD)
        self.staff.save()

    # ---------- AC-11 ----------
    def test_ac11_operator_autofill(self):
        """AC-11: 送样登记人字段自动填充当前用户且只读；试样/终止表单同步。"""
        cs = self.login(self.staff)
        html = cs.get(reverse("samples:create")).content.decode()
        self.assertIn(self.staff.username, html)
        self.assertIn("readonly", html)
        # 终止表单隐藏自由输入并静态展示操作人
        from samples.models import Sample
        sample = Sample.objects.filter(is_deleted=False).first()
        if sample:
            html = cs.get(reverse("samples:terminate", args=[sample.pk])).content.decode()
            self.assertIn("由系统自动记录", html)

    # ---------- AC-12 ----------
    def test_ac12_coverage_completeness(self):
        """AC-12: 静态审计无漏网视图；无角色用户 GET 全站业务 URL 无一 200。"""
        from records import urls as records_urls
        from knowledge_base import urls as kb_urls
        from samples import urls as samples_urls
        from ai_api import urls as api_urls

        # home：按可见模块裁剪内容，不加单模块装饰器（否则只有档案权限的用户打不开首页）
        # search：跨模块页面，用 @require_any_module（零权限 403）
        records_whitelist = {"home", "search", "test_view"}
        checked = 0
        for p in records_urls.urlpatterns:
            if p.name in records_whitelist:
                continue
            self.assertTrue(getattr(p.callback, "rbac_permission", None), f"records:{p.name}")
            checked += 1
        search_view = next(p.callback for p in records_urls.urlpatterns if p.name == "search")
        self.assertTrue(getattr(search_view, "rbac_any_module", False), "search 必须有跨模块权限判定")
        for conf, label in ((kb_urls, "kb"), (samples_urls, "samples")):
            for p in conf.urlpatterns:
                self.assertTrue(getattr(p.callback, "rbac_permission", None), f"{label}:{p.name}")
                checked += 1
        for entry in api_urls.router.registry:
            viewset = entry[1]
            self.assertTrue(getattr(viewset, "perm_module", None))
            checked += 1
        self.assertGreaterEqual(checked, 53 + 10 + 27 + 9)

        # 无角色用户遍历全站业务 GET URL，均不得 200
        cn = self.login(self.norole)
        urls = [
            reverse("daily_record_list"), reverse("decoration_list"),
            reverse("customer_credit_list"), reverse("customer_complaint_list"),
            reverse("regulation_list"), reverse("price_evaluation_list"),
            reverse("insurance_list"), reverse("complaint_list"),
            reverse("dashboard"), reverse("data_download"), reverse("operation_log_list"),
            reverse("knowledge_base:document_list"), reverse("knowledge_base:category_list"),
            reverse("samples:list"), reverse("samples:dashboard"),
            reverse("samples:partner_list"), reverse("samples:log_list"),
            reverse("accounts:user_list"), reverse("accounts:role_list"),
            reverse("accounts:login_logs"),
            reverse("expiry_calendar"), reverse("expiry_export"),
            reverse("weekly_report"), reverse("annual_summary"),
            reverse("audit_anomalies"), reverse("system_health"),
        ]
        for path in urls:
            self.assertNotEqual(cn.get(path).status_code, 200, path)
        # 首页：可访问，但只应看到"无权限"空态，不能出现任何业务数据块
        home_resp = cn.get(reverse("home"))
        self.assertEqual(home_resp.status_code, 200)
        home_html = home_resp.content.decode()
        self.assertIn("还没有任何模块权限", home_html)
        # 断言"数据块"而不是模块名：模板注释里仍有「客户账期卡片」这类字样
        self.assertNotIn("card-value", home_html)
        self.assertNotIn("总记录数", home_html)
        self.assertNotIn("总客户数", home_html)
        # 全站搜索：零权限直接 403（不再是"能打开但全空"）
        self.assertEqual(cn.get(reverse("search")).status_code, 403)
