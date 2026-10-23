# -*- coding: utf-8 -*-
"""长期 Token 功能验收测试。

覆盖四类关注点：
1. 页面权限控制（匿名 302 / 非管理员 403 / 管理员 200 / 侧栏入口显隐）；
2. 签发接口 /api-token-auth/（会话换取、口令换取、越权、口令爆破锁定）；
3. DRF 全局 Token 认证（有效 / 无效 / 过期 / 撤销 Token，且仍受 RBAC 约束）；
4. 历史记录列表的分页与按用户隔离；
5. 撤销入口（仅 POST、仅本人，越权落 404，撤销后立即失效）；
6. 改密 / 管理员重置后 Token 失效联动。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.exceptions import FieldError
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from rest_framework.test import APIClient

from .models import ApiToken, Role, _hash_token_key

PWD = "X12345678"
TOKEN_URL = "/api-token-auth/"
DECORATION_API = "/api/decoration/"
DAILY_RECORD_API = "/api/daily-record/"


class ApiTokenFeatureTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("sync_rbac", verbosity=0)
        cls.admin = User.objects.create_user("admin_tk", "a@e.com", PWD, is_active=True)
        cls.admin.rbac_roles.add(Role.objects.get(code="admin"))
        cls.staff = User.objects.create_user("staff_tk", "s@e.com", PWD, is_active=True)
        cls.staff.rbac_roles.add(Role.objects.get(code="staff"))
        cls.viewer = User.objects.create_user("viewer_tk", "v@e.com", PWD, is_active=True)
        cls.viewer.rbac_roles.add(Role.objects.get(code="viewer"))
        cls.superuser = User.objects.create_superuser("root_tk", "r@e.com", PWD)

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    # ---------- 辅助 ----------
    def _session_client(self, user):
        """已登录且携带 csrftoken cookie 的客户端（触发一次页面渲染以种下 cookie）。"""
        c = Client()
        c.force_login(user)
        c.get(reverse("accounts:api_token"))
        return c

    def _post_session(self, client, url, data=None):
        morsel = client.cookies.get("csrftoken")
        kwargs = {"HTTP_X_CSRFTOKEN": morsel.value} if morsel else {}
        return client.post(url, data or {}, **kwargs)

    def _token_client(self, key):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Token " + key)
        return client

    # ---------- 1. 页面权限 ----------
    def test_page_anonymous_redirects_to_login(self):
        resp = Client().get(reverse("accounts:api_token"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp["Location"])

    def test_page_forbidden_for_staff(self):
        c = Client()
        c.force_login(self.staff)
        self.assertEqual(c.get(reverse("accounts:api_token")).status_code, 403)

    def test_page_ok_for_admin_and_superuser(self):
        for user in (self.admin, self.superuser):
            c = Client()
            c.force_login(user)
            resp = c.get(reverse("accounts:api_token"))
            self.assertEqual(resp.status_code, 200)
            self.assertContains(resp, "长期 Token")
            self.assertContains(resp, "获取最新长期Token")

    def test_sidebar_entry_visibility(self):
        """侧栏「长期Token」入口：管理员可见、普通员工不可见。"""
        c_admin = Client()
        c_admin.force_login(self.admin)
        self.assertContains(c_admin.get(reverse("home")), reverse("accounts:api_token"))

        c_staff = Client()
        c_staff.force_login(self.staff)
        self.assertNotContains(c_staff.get(reverse("home")), reverse("accounts:api_token"))

    # ---------- 2. 签发接口 ----------
    def test_issue_with_session(self):
        c = self._session_client(self.admin)
        resp = self._post_session(c, TOKEN_URL)
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertTrue(data["key"])
        self.assertTrue(data["key_prefix"])
        self.assertEqual(data["status"], "active")
        self.assertEqual(ApiToken.objects.filter(user=self.admin).count(), 1)
        # 明文只在签发响应里出现一次：历史列表能认得前缀，却拿不到明文
        page = c.get(reverse("accounts:api_token")).content.decode()
        self.assertIn(data["key_prefix"], page)
        self.assertNotIn(data["key"], page)

    def test_issue_with_credentials_anonymous(self):
        resp = Client().post(TOKEN_URL, {"username": "admin_tk", "password": PWD})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(ApiToken.objects.filter(user=self.admin).count(), 1)

    def test_issue_wrong_password(self):
        resp = Client().post(TOKEN_URL, {"username": "admin_tk", "password": "bad"})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(ApiToken.objects.count(), 0)

    def test_issue_missing_credentials(self):
        self.assertEqual(Client().post(TOKEN_URL, {}).status_code, 400)

    def test_issue_forbidden_for_non_admin(self):
        # 会话方式：普通员工无 system.update
        c = self._session_client(self.staff)
        self.assertEqual(self._post_session(c, TOKEN_URL).status_code, 403)
        # 口令方式
        resp = Client().post(TOKEN_URL, {"username": "staff_tk", "password": PWD})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(ApiToken.objects.count(), 0)

    def test_issue_lockout_after_repeated_failures(self):
        c = Client()
        for _ in range(5):
            c.post(TOKEN_URL, {"username": "admin_tk", "password": "bad"})
        resp = c.post(TOKEN_URL, {"username": "admin_tk", "password": PWD})
        self.assertEqual(resp.status_code, 429)

    # ---------- 3. DRF 全局 Token 认证 ----------
    def test_token_authenticates_drf_api(self):
        token = ApiToken.issue(self.admin, ip="127.0.0.1")
        self.assertEqual(self._token_client(token.raw_key).get(DECORATION_API).status_code, 200)

    def test_invalid_token_rejected(self):
        self.assertEqual(self._token_client("not-a-real-token").get(DECORATION_API).status_code, 401)

    def test_expired_token_rejected(self):
        token = ApiToken.issue(self.admin)
        token.expires_at = timezone.now() - timedelta(days=1)
        token.save(update_fields=["expires_at"])
        self.assertEqual(self._token_client(token.raw_key).get(DECORATION_API).status_code, 401)

    def test_revoked_token_rejected(self):
        token = ApiToken.issue(self.admin)
        token.is_active = False
        token.save(update_fields=["is_active"])
        self.assertEqual(self._token_client(token.raw_key).get(DECORATION_API).status_code, 401)

    def test_token_still_bound_by_rbac(self):
        """Token 仅代表身份，权限仍按该用户 RBAC 判定：只读角色不可写。"""
        token = ApiToken.issue(self.viewer)
        client = self._token_client(token.raw_key)
        self.assertEqual(client.get(DAILY_RECORD_API).status_code, 200)
        resp = client.post(
            DAILY_RECORD_API,
            {"department": "d", "work_content": "x"},
            format="json",
        )
        self.assertEqual(resp.status_code, 403)

    def test_token_invalidated_by_password_change(self):
        """改密后既有 Token 失效：指纹与当前密码不一致即拒绝。"""
        token = ApiToken.issue(self.admin)
        self.admin.set_password("BrandNewPwd456")
        self.admin.save()
        self.assertEqual(self._token_client(token.raw_key).get(DECORATION_API).status_code, 401)

    # ---------- 4. 历史记录 ----------
    # ---------- P1-2：明文不落库 ----------
    def test_plaintext_key_is_not_persisted(self):
        """审计 P1-2：明文 key 不得出现在数据库任何字段里。"""
        token = ApiToken.issue(self.admin, ip="127.0.0.1")
        raw = token.raw_key
        stored = ApiToken.objects.get(pk=token.pk)
        # 模型上已无 key 字段；状态字典里也不该有它
        self.assertNotIn("key", stored.__dict__)
        self.assertEqual(stored.key_prefix, raw[:12])
        self.assertEqual(stored.key_hash, _hash_token_key(raw))
        # 整行序列化后仍找不到明文（防以后有人加字段把明文又存回去）
        row_text = repr(stored.__dict__)
        self.assertNotIn(raw, row_text)

    def test_hash_only_authentication_still_works(self):
        """明文的唯一出口是签发响应：认证按哈希，明文列已不存在。"""
        token = ApiToken.issue(self.admin)
        # 认证成功
        self.assertEqual(self._token_client(token.raw_key).get(DECORATION_API).status_code, 200)
        # 查不到明文列：任何「按 key=明文 查询」的写法都该直接 AttributeError
        with self.assertRaises(FieldError):
            ApiToken.objects.filter(key=token.raw_key).exists()

    def test_page_never_leaks_full_key(self):
        """历史列表与签发后的刷新页面都不含明文（前缀除外）。"""
        token = ApiToken.issue(self.admin)
        c = self._session_client(self.admin)
        page = c.get(reverse("accounts:api_token")).content.decode()
        self.assertNotIn(token.raw_key, page)
        self.assertIn(token.key_prefix, page)

    def test_history_pagination(self):
        for _ in range(12):
            ApiToken.issue(self.admin)
        c = Client()
        c.force_login(self.admin)
        resp = c.get(reverse("accounts:api_token"))
        self.assertEqual(resp.status_code, 200)
        page1 = resp.context["page"]
        self.assertEqual(page1.paginator.per_page, 10)
        self.assertEqual(page1.paginator.num_pages, 2)
        self.assertEqual(len(page1.object_list), 10)

    def test_history_isolated_per_user(self):
        ApiToken.issue(self.admin)
        c = Client()
        c.force_login(self.superuser)
        resp = c.get(reverse("accounts:api_token"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["total"], 0)

    def test_page_marks_password_changed_as_revoked(self):
        """改密后历史列表该行状态应显示「已撤销」，并撤下撤销按钮。"""
        token = ApiToken.issue(self.admin)
        self.admin.set_password("AnotherPwd789")
        self.admin.save()
        c = self._session_client(self.admin)
        resp = c.get(reverse("accounts:api_token"))
        self.assertContains(resp, "已撤销")
        self.assertNotContains(resp, reverse("accounts:api_token_revoke", args=[token.pk]))

    # ---------- 5. 撤销入口 ----------
    def _revoke_url(self, token):
        return reverse("accounts:api_token_revoke", args=[token.pk])

    def test_revoke_own_token(self):
        c = self._session_client(self.admin)
        token = ApiToken.issue(self.admin)
        resp = self._post_session(c, self._revoke_url(token))
        self.assertEqual(resp.status_code, 302)
        token.refresh_from_db()
        self.assertFalse(token.is_active)
        # 撤销后该 Token 调用立即失败
        self.assertEqual(self._token_client(token.raw_key).get(DECORATION_API).status_code, 401)

    def test_revoke_page_shows_button_for_active_only(self):
        c = self._session_client(self.admin)
        active = ApiToken.issue(self.admin)
        revoked = ApiToken.issue(self.admin)
        revoked.is_active = False
        revoked.save(update_fields=["is_active"])
        resp = c.get(reverse("accounts:api_token"))
        self.assertContains(resp, self._revoke_url(active))
        self.assertNotContains(resp, self._revoke_url(revoked))

    def test_revoke_other_users_token_not_allowed(self):
        """越权撤销他人 Token：即使是超管也按本人隔离，落到 404，Token 保持不变。"""
        c = self._session_client(self.superuser)
        token = ApiToken.issue(self.admin)
        resp = self._post_session(c, self._revoke_url(token))
        self.assertEqual(resp.status_code, 404)
        token.refresh_from_db()
        self.assertTrue(token.is_active)

    def test_revoke_requires_post(self):
        c = self._session_client(self.admin)
        token = ApiToken.issue(self.admin)
        self.assertEqual(c.get(self._revoke_url(token)).status_code, 405)

    def test_revoke_anonymous_redirects_to_login(self):
        token = ApiToken.issue(self.admin)
        resp = Client().post(self._revoke_url(token))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp["Location"])
        token.refresh_from_db()
        self.assertTrue(token.is_active)

    def test_revoke_forbidden_for_non_admin(self):
        c = self._session_client(self.staff)
        token = ApiToken.issue(self.admin)
        self.assertEqual(self._post_session(c, self._revoke_url(token)).status_code, 403)
        token.refresh_from_db()
        self.assertTrue(token.is_active)
