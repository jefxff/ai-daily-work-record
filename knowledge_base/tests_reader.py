"""知识库详情页「书房阅读器」验收测试（frontend-design 改造批次）。

覆盖本次改造的四个问题：
1. Markdown 阅读排版：标题/表格/引用/代码块正常渲染，GFM 任务列表转 checkbox；
   防回归重点：详情页自带 extra_css 会整块覆盖基座同名 block，
   `.markdown-content` 规则必须出现在本页样式里（曾经的「不美观」根因）。
2. 思维导图舞台：画布容器 + 工具条（展开/折叠/缩放）就位，不再是小框。
3. 返回列表按钮：位于文档操作组第一位（「设为全员可见」旁边）。
4. 文件夹系统：左栏分类树渲染；私有文档标题绝不泄漏给他人；
   列表页右栏分类筛选同为树形，选中子分类时祖先链默认展开。

运行：python manage.py test knowledge_base.tests_reader
"""
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse

from accounts.models import Permission, Role
from accounts.registry import permission_code

from .models import KnowledgeCategory, KnowledgeDocument

PWD = "X12345678"


def grant(user, *pairs):
    """按 (module, action) 授权（与 tests.py 同一约定）。"""
    role = Role.objects.create(code=f"kb-reader-{user.username}", name="阅读器测试角色")
    for module, action in pairs:
        perm, _ = Permission.objects.get_or_create(
            module=module, action=action,
            defaults={"code": permission_code(module, action), "name": f"{module}-{action}"},
        )
        role.permissions.add(perm)
    role.users.add(user)
    if hasattr(user, "_rbac_codes_cache"):
        del user._rbac_codes_cache
    cache.clear()


class ReaderShellTests(TestCase):
    """三栏骨架 + 视图切换 + 工具条的结构契约。"""

    def setUp(self):
        self.parent = KnowledgeCategory.objects.create(name="系列教程", sort_order=1)
        self.child = KnowledgeCategory.objects.create(
            name="入门篇", parent=self.parent, sort_order=1)
        self.doc = KnowledgeDocument.objects.create(
            title="书房测试文档", category=self.child,
            content="# 大标题\n\n## 小节\n\n正文一段。\n\n"
                    "- [x] 已完成项\n- [ ] 未完成项\n\n"
                    "> 引用一句\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n",
            summary="摘要一句话", status="published", visibility="public",
        )
        self.user = User.objects.create_user("kbhome", "kb@e.com", PWD, is_active=True)
        grant(self.user, ("knowledge", "read"), ("knowledge", "update"))
        self.client = Client()
        self.client.force_login(self.user)
        self.html = self.client.get(
            reverse("knowledge_base:document_detail", args=[self.doc.pk])).content.decode("utf-8")

    def tearDown(self):
        cache.clear()

    def test_three_column_shell_renders(self):
        for marker in ("kb-reader", "kb-folder-tree", "kb-reader-main",
                       "kb-view-switch", "kbMindmapCanvas"):
            self.assertIn(marker, self.html, f"缺少骨架标记 {marker}")

    def test_view_panels_default_state(self):
        """阅读视图默认可见，大纲/导图隐藏；分段控件三个 tab 就位。"""
        self.assertIn('id="kbReadView" role="tabpanel"', self.html)
        self.assertIn('id="kbOutlineView" class="kb-outline" role="tabpanel" hidden', self.html)
        self.assertIn('id="kbMindmapView" role="tabpanel" hidden', self.html)
        for view in ("read", "outline", "mindmap"):
            self.assertIn(f'data-view="{view}"', self.html)

    def test_mindmap_toolbar_present(self):
        """导图不再是裸画布：展开/折叠/缩放工具条 + 缩放读数。"""
        self.assertIn('data-mm-action="expand"', self.html)
        self.assertIn('data-mm-action="collapse"', self.html)
        self.assertIn('data-mm-action="zoom-in"', self.html)
        self.assertIn('data-mm-action="zoom-out"', self.html)
        self.assertIn('id="kbMmZoom"', self.html)

    def test_markdown_styles_defined_on_detail_page(self):
        """防回归：基座的 .markdown-content 规则不会继承过来（同名 block 被整块覆盖），
        本页 extra_css 必须自带这套排版规则。"""
        self.assertIn(".markdown-content h1", self.html)
        self.assertIn(".markdown-content blockquote", self.html)
        self.assertIn(".markdown-content table", self.html)
        self.assertIn(".kb-mm-box.is-root", self.html)
        self.assertIn(".kb-outline-row.is-h1", self.html)

    def test_back_button_first_in_actions(self):
        """问题 3：返回列表在操作组第一位（设为全员可见旁边）。"""
        actions = self.html[self.html.find('class="kb-doc-actions"'):]
        actions = actions[:actions.find("</header>")]
        self.assertLess(actions.find("返回列表"), actions.find("kbVisToggle"))
        self.assertLess(actions.find("返回列表"), actions.find("编辑大纲"))

    def test_breadcrumb_shows_category_path(self):
        """面包屑：知识库 / 系列教程 / 入门篇。"""
        self.assertIn('class="kb-crumb"', self.html)
        self.assertIn("?category=%d" % self.parent.pk, self.html)
        self.assertIn("?category=%d" % self.child.pk, self.html)


class MarkdownRenderingTests(TestCase):
    """问题 1：Markdown 阅读格式（含 GFM 任务列表）。"""

    def setUp(self):
        self.doc = KnowledgeDocument.objects.create(
            title="Markdown 渲染", content=(
                "# 一级标题\n\n正文。\n\n"
                "## 二级标题\n\n"
                "- [x] 勾选项\n- [ ] 未勾选项\n- 普通项\n\n"
                "> 引用内容\n\n"
                "```python\nprint('hi')\n```\n\n"
                "| 列一 | 列二 |\n| --- | --- |\n| 甲 | 乙 |\n"
            ), status="published", visibility="public",
        )
        self.user = User.objects.create_user("kbmd", "md@e.com", PWD, is_active=True)
        grant(self.user, ("knowledge", "read"))
        self.client = Client()
        self.client.force_login(self.user)
        resp = self.client.get(reverse("knowledge_base:document_detail", args=[self.doc.pk]))
        self.body = resp.content.decode("utf-8")
        start = self.body.find('id="kbReadView"')
        end = self.body.find('id="kbOutlineView"')
        self.read = self.body[start:end]

    def tearDown(self):
        cache.clear()

    def test_structural_markdown_elements(self):
        self.assertIn("<h1", self.read)
        self.assertIn("<h2", self.read)
        self.assertIn("<blockquote", self.read)
        self.assertIn("<pre", self.read)   # codehilite 扩展会带 class
        self.assertIn("<table", self.read)
        self.assertIn("<code", self.read)

    def test_task_list_becomes_disabled_checkbox(self):
        """`- [x]`/`- [ ]` → 只读 checkbox（阅读态不写库）。"""
        self.assertEqual(self.read.count('class="kb-task-item"'), 2)
        self.assertIn('<input type="checkbox" disabled checked>', self.read)
        self.assertIn('<input type="checkbox" disabled>', self.read)

    def test_plain_list_item_keeps_marker(self):
        self.assertIn("普通项", self.read)
        self.assertNotIn("[ ]", self.read)


class FolderTreeTests(TestCase):
    """问题 4：左栏文件夹树与私有文档隔离。"""

    def setUp(self):
        self.series = KnowledgeCategory.objects.create(name="Django 学习", sort_order=5)
        self.basic = KnowledgeCategory.objects.create(
            name="基础篇", parent=self.series, sort_order=1)
        self.public_doc = KnowledgeDocument.objects.create(
            title="公开教程", category=self.basic, content="公开内容",
            status="published", visibility="public")
        self.owner = User.objects.create_user("kbowner", "owner@e.com", PWD, is_active=True)
        self.stranger = User.objects.create_user("kbstrange", "s@e.com", PWD, is_active=True)
        grant(self.owner, ("knowledge", "read"))
        grant(self.stranger, ("knowledge", "read"))
        self.own_private = KnowledgeDocument.objects.create(
            title="我的私有笔记", category=self.basic, content="私有内容",
            status="published", visibility="private", owner=self.owner)
        self.other_private = KnowledgeDocument.objects.create(
            title="他人的私有笔记", category=self.basic, content="他人私有",
            status="published", visibility="private", owner=self.stranger)

    def tearDown(self):
        cache.clear()

    def _tree_html(self, user, doc):
        c = Client()
        c.force_login(user)
        html = c.get(reverse("knowledge_base:document_detail", args=[doc.pk])).content.decode("utf-8")
        start = html.find('class="kb-folder-tree"')
        return html[start:html.find('class="kb-reader-main"')]

    def test_tree_shows_series_and_docs(self):
        tree = self._tree_html(self.owner, self.public_doc)
        self.assertIn("Django 学习", tree)
        self.assertIn("基础篇", tree)
        self.assertIn("公开教程", tree)

    def test_tree_hides_other_users_private_docs(self):
        """铁律：他人私有文档标题绝不经过文件夹树泄漏。"""
        tree = self._tree_html(self.owner, self.public_doc)
        self.assertNotIn("他人的私有笔记", tree)

    def test_tree_shows_own_private_doc_with_lock(self):
        tree = self._tree_html(self.owner, self.public_doc)
        self.assertIn("我的私有笔记", tree)
        self.assertIn("kb-folder-doc-lock", tree)

    def test_current_doc_marked_in_tree(self):
        tree = self._tree_html(self.owner, self.public_doc)
        self.assertIn("kb-folder-doc is-current", tree)
        self.assertIn('aria-current="page"', tree)

    def test_uncategorized_group_renders(self):
        loose = KnowledgeDocument.objects.create(
            title="游离文档", content="无分类", status="published", visibility="public")
        tree = self._tree_html(self.owner, loose)
        self.assertIn("未分类", tree)
        self.assertIn("游离文档", tree)


class ListCategoryTreeTests(TestCase):
    """列表页右栏分类筛选树形化。"""

    def setUp(self):
        self.root = KnowledgeCategory.objects.create(name="总分类", sort_order=1)
        self.leaf = KnowledgeCategory.objects.create(
            name="子分类", parent=self.root, sort_order=1)
        KnowledgeDocument.objects.create(
            title="树下文档", category=self.leaf, content="内容",
            status="published", visibility="public")
        self.user = User.objects.create_user("kbtree", "tree@e.com", PWD, is_active=True)
        grant(self.user, ("knowledge", "read"))
        self.client = Client()
        self.client.force_login(self.user)

    def tearDown(self):
        cache.clear()

    def test_tree_renders_with_caret_and_counts(self):
        html = self.client.get(reverse("knowledge_base:document_list")).content.decode("utf-8")
        self.assertIn("kb-cat-tree", html)
        self.assertIn("kb-cat-caret", html)
        self.assertIn("总分类", html)
        self.assertIn("子分类", html)

    def test_ancestors_expand_when_child_selected(self):
        """选中子分类：父链默认展开，子项高亮。"""
        resp = self.client.get(
            reverse("knowledge_base:document_list"), {"category": self.leaf.pk})
        html = resp.content.decode("utf-8")
        seg = html[html.find('class="kb-cat-tree"'):]   # 属性锚点，避开 <style> 里的选择器
        seg = seg[:seg.find("标签云")]
        self.assertIn("is-open", seg)               # 父节点 caret 展开态
        self.assertNotIn(" hidden", seg)            # 子树容器未折叠
        self.assertIn("active", seg)                # 选中高亮


class VisibilityToggleContractTests(TestCase):
    """可见性切换的 DOM 契约（被历史测试依赖，重构不得破坏）。"""

    def setUp(self):
        self.doc = KnowledgeDocument.objects.create(
            title="契约文档", content="内容", status="published", visibility="private",
        )
        self.user = User.objects.create_user("kbcontract", "c@e.com", PWD, is_active=True)
        grant(self.user, ("knowledge", "read"), ("knowledge", "update"))
        self.doc.owner = self.user
        self.doc.save()
        self.client = Client()
        self.client.force_login(self.user)
        self.html = self.client.get(
            reverse("knowledge_base:document_detail", args=[self.doc.pk])).content.decode("utf-8")

    def tearDown(self):
        cache.clear()

    def test_toggle_ids_and_labels(self):
        for marker in ('id="kbVisBadge"', 'id="kbVisToggle"', 'id="kbVisBadgeText"',
                       'id="kbVisToggleText"'):
            self.assertIn(marker, self.html)
        self.assertIn("设为全员可见", self.html)
        self.assertIn("仅自己可见", self.html)
