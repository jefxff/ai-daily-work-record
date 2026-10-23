"""全站权限覆盖守卫（审计 P2-4）。

原守卫 `accounts/tests.py::test_ac12_coverage_completeness` 只遍历 records /
knowledge_base / samples 三个 app 的 urlconf 加 ai_api 的 router；archive、
accounts、notifications、sysbackup 的函数视图不在断言范围内。实测这些模块
**当前**都做了判定，但没有自动化兜底：下一个新增视图忘了加装饰器，测试不会红。

这里把范围扩到**全站 urlconf**（含 include 嵌套、DRF viewset、DRF router），
判定依据是视图回调上的标记属性：

- `rbac_permission`   → @perm_required / @perm_required_any
- `rbac_any_module`   → @require_any_module（跨模块页面）
- `rbac_public`       → @login_exempt（匿名可达，视图内自行鉴权）
- DRF viewset 类上的 `perm_module` + permission_classes

不满足上述任一条件的业务视图，必须出现在下面这张**带理由的白名单**里。
白名单双向校验：新增未标记视图 → 测试红；白名单条目过期（视图已删除或已补标记）
→ 同样红，逼你删掉过期条目而不是让名单无限膨胀。
"""

from django.test import SimpleTestCase
from django.urls import URLPattern, URLResolver, get_resolver

# 显式豁免清单：视图名 → 为什么它不需要 rbac 标记。
# 每条都必须能在遍历结果里找到；找不到说明条目过期，测试会失败。
WHITELIST = {
    # —— 登录/登出/注册：本就必须匿名可达或仅作用于自身会话 ——
    "accounts.views_auth.login_view": "登录页必须匿名可达（防爆破锁定在 accounts.security）",
    "accounts.views_auth.logout_view": "仅注销当前用户自己的会话",
    "accounts.views_auth.register_view": "自助注册：限流 + 只建未激活账号",
    # —— 自助功能：只作用于 request.user 本人，登录由 LoginRequiredMiddleware 保证 ——
    "accounts.views_self.password_change_view": "自助改密，仅作用于当前用户",
    "accounts.views_self.password_done_view": "改密成功提示页，仅作用于当前用户",
    "accounts.views_self.session_list_view": "我的会话列表，仅读本人会话",
    "accounts.views_self.session_revoke_view": "撤销我的某个会话，按 session_key 归属校验",
    "accounts.views_self.session_revoke_others_view": "撤销我的其它会话，仅作用于本人",
    # —— 首页：按可见模块裁剪，零权限账号走空态 ——
    "records.views_home.home": "首页按 visible_modules 裁剪内容；登录由 LoginRequiredMiddleware 保证",
    # —— 媒体下载：三层校验在视图内部完成 ——
    "work_record.urls.protected_media": "媒体下载在视图内做模块归属/登录/存在性三层校验",
    # —— DRF：权限挂在 permission_classes / 视图内部 _can 判定上 ——
    "rest_framework.routers.view": "DRF router 根视图（接口清单收紧项见审计 P3-1）",
    "ai_api.views_ai.view": "DRF @api_view + permission_classes(IsAuthenticated)，模块判定在视图内 _can",
    # —— 高危动作收紧到超管（与备份下载同一思路）——
    "notifications.views.scheduled_task_run": "@_superuser_required，超管专属",
    "notifications.views.scheduled_task_update": "@_superuser_required，超管专属",
}


def _walk(resolver, prefix=""):
    for entry in resolver.url_patterns:
        if isinstance(entry, URLResolver):
            yield from _walk(entry, prefix + str(entry.pattern))
        elif isinstance(entry, URLPattern):
            yield prefix + str(entry.pattern), entry


def _view_class_of(callback):
    return getattr(callback, "view_class", None) or getattr(callback, "cls", None)


def _has_marker(callback, view_class):
    if any(
        getattr(callback, attr, None)
        for attr in ("rbac_permission", "rbac_any_module", "rbac_public")
    ):
        return True
    # DRF viewset：权限挂在类上
    return bool(view_class is not None and getattr(view_class, "perm_module", None))


def _is_framework(module):
    return module.startswith("django.") or "django.contrib" in module or "debug_toolbar" in module


class RbacCoverageTests(SimpleTestCase):
    """全站业务视图必须带权限标记，或在带理由的白名单内。"""

    def _collect(self):
        rows = []
        for pattern, entry in _walk(get_resolver()):
            callback = entry.callback
            view_class = _view_class_of(callback)
            # 用 __name__ 而非 __qualname__：DRF 的 @api_view 包装函数 __name__ 稳定为
            # "view"，而 __qualname__ 会随内层类变化（View.as_view 之类），对不上白名单
            qualname = f"{getattr(callback, '__module__', '')}.{getattr(callback, '__name__', type(callback).__name__)}"
            rows.append({
                "pattern": pattern,
                "name": entry.name or "",
                "qualname": qualname,
                "module": getattr(callback, "__module__", "") or "",
                "marked": _has_marker(callback, view_class),
                "viewset": view_class is not None,
            })
        return rows

    def test_every_business_view_is_guarded_or_whitelisted(self):
        rows = self._collect()
        business = [r for r in rows if not _is_framework(r["module"])]
        # 规模 sanity check：遍历真的覆盖到了全站（防 resolver 结构变化导致静默漏扫）
        self.assertGreater(len(rows), 300, f"只遍历到 {len(rows)} 条 URL，urlconf 结构可能变了")
        self.assertGreater(len(business), 250)

        offenders = [
            f"{r['pattern']}  ->  {r['qualname']}"
            for r in business
            if not r["marked"] and r["qualname"] not in WHITELIST
        ]
        self.assertEqual(
            offenders, [],
            "以下业务视图没有 rbac_permission / rbac_any_module / rbac_public 标记，"
            "也不在 tests_rbac_coverage.WHITELIST 里：\n" + "\n".join(offenders),
        )

    def test_whitelist_has_no_stale_entries(self):
        """白名单条目必须都能在遍历结果里找到，否则就是过期条目，应删除。"""
        seen = {r["qualname"] for r in self._collect()}
        stale = sorted(k for k in WHITELIST if k not in seen)
        self.assertEqual(
            stale, [],
            "WHITELIST 里有已不存在/已改名的视图（删掉这些条目）：\n" + "\n".join(stale),
        )

    def test_whitelist_entries_have_reasons(self):
        empty = [k for k, v in WHITELIST.items() if not v or not v.strip()]
        self.assertEqual(empty, [], "白名单条目必须写豁免理由：" + ", ".join(empty))

    def test_drf_viewsets_are_marked_via_class(self):
        """DRF viewset 的权限必须挂在类上（perm_module），逐个点名确认。

        白名单条目（如 DRF router 根视图）不在此列：它的问题已在审计 P3-1
        记录为待收紧项，收紧前靠白名单显式豁免。
        """
        viewsets = [
            r for r in self._collect()
            if r["viewset"] and not _is_framework(r["module"]) and r["qualname"] not in WHITELIST
        ]
        self.assertTrue(viewsets, "没有遍历到任何 viewset，检查 _view_class_of 是否失效")
        for r in viewsets:
            self.assertTrue(
                r["marked"],
                f"{r['pattern']} 的 viewset 类没有 perm_module 标记",
            )
