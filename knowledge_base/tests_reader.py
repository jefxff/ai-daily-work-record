"""知识库详情页「书房阅读器」验收测试（frontend-design 改造批次）。

覆盖本次改造的四个问题：
1. Markdown 阅读排版：标题/表格/引用/代码块正常渲染，GFM 任务列表转 checkbox；
   防回归重点：详情页自带 extra_css 会整块覆盖基座同名 block，
   `.markdown-content` 规则必须出现在本页样式里（曾经的「不美观」根因）。
2. 思维导图：画布尺寸取全部节点盒子的并集（旧 `_subH` 算法把高度算成
   47px，下方节点全被 viewBox 裁掉——2026-10-22 用户截图「只剩一个 pill、
   大片空白」）；markdown 扁平树按标题层级分组；小图自动放大铺满舞台。
3. 返回列表按钮：位于文档操作组第一位（「设为全员可见」旁边）。
4. 详情页单栏 + 右栏：原左栏文件夹系统已取消，改为与列表页共用的
   右栏分类筛选树（_right_sidebar.html）；caret 折叠/展开即文件夹能力——
   每个有内容的分类（子分类或可见文档）都有 caret，展开区 = 文档 +
   子分类树；当前文档所在分类高亮且祖先链默认展开；他人私有文档标题
   绝不泄漏（不进树），自己的私有文档带锁标；计数口径与列表一致。
   防回归重点：详情页 extra_css 必须 `{{ block.super }}`，否则基座的
   右栏布局样式被整块覆盖，右栏会掉到文档流下方。

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
    """单栏阅读区 + 右栏骨架 + 视图切换 + 工具条的结构契约。

    详情页原左栏文件夹系统已取消（用户反馈三栏不美观、文件夹与列表页
    分类重复）：正文区单栏铺开，右栏与列表页共用 _right_sidebar.html。
    """

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

    def test_single_column_with_right_sidebar(self):
        """单栏正文 + 右栏（统计/分类筛选树/标签云），不再有左栏文件夹。"""
        for marker in ("kb-reader-body", "kb-view-switch", "kbMindmapCanvas",
                       "fixed-right-sidebar", "kb-cat-tree", "kb-side-head",
                       "kb-stats-grid", "kb-side-section", "标签云"):
            self.assertIn(marker, self.html, f"缺少骨架标记 {marker}")
        # 左栏文件夹系统已取消，不得回流
        self.assertNotIn("kb-folder-tree", self.html)
        self.assertNotIn("kb-reader-main", self.html)

    def test_base_layout_css_inherited(self):
        """防回归：详情页 extra_css 必须 {{ block.super }}。基座同名 block 里
        有右栏布局（.fixed-right-sidebar / .knowledge-main-with-right）与侧栏
        样式，整块覆盖会让右栏掉到文档流下方（2026-10-22 用户截图 2 事故）。"""
        self.assertIn(".fixed-right-sidebar", self.html)
        self.assertIn("position: fixed", self.html)
        self.assertIn(".knowledge-main-with-right", self.html)
        self.assertIn(".kb-cat-doc", self.html)   # 侧栏文档行样式同样来自基座

    def test_caret_toggle_script_present(self):
        """右栏 caret 折叠脚本在基座（两页共用）——放在列表页 extra_js 里会被
        详情页同名 block 覆盖，详情页的 caret 曾是一排死按钮（2026-10-22）。"""
        self.assertIn("kb-cat-caret:not(.kb-cat-caret-spacer)", self.html)

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

    def test_mindmap_stage_fits_content(self):
        """问题 1（2026-10-22 三修）：画布尺寸取全部节点盒子的并集——旧
           `_subH` 算法把多节点画布高度算成 47px，根与后续节点全被 viewBox
           裁掉（用户看到「内容没有正确显示，只剩一个 pill、大片空白」）；
           markdown 扁平树按标题层级分组（段落不进导图）；小图自动放大
           铺满舞台宽度并水平居中；空态紧凑舞台。"""
        self.assertIn("buildMapTree", self.html)       # 扁平树 → 标题层级树
        self.assertIn("editor_mode", self.html)        # outline 模式直通的判断
        self.assertIn("minTop", self.html)             # 盒子并集边界（替换 _subH）
        self.assertIn("offsetY", self.html)            # 整体下移，防父盒探出上沿
        self.assertIn("mmState.autoScale", self.html)  # 小图自适应放大
        self.assertIn("SCALE_MAX", self.html)
        self.assertNotIn("node._subH", self.html)   # 旧算法代码不得回流（注释提及不算）
        self.assertIn("min-height: 56vh", self.html)   # 大图：交还 CSS 滚动
        self.assertIn("margin-left: auto", self.html)  # 水平居中

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


class DetailSidebarTests(TestCase):
    """问题 4（2026-10-22 改版）：详情页右栏 = 列表页右栏，分类树的
    caret 折叠/展开即文件夹系统——展开区 = 该分类的可见文档 + 子分类树。

    私有隔离口径：树里的文档只有当前用户可见的（他人私有文档不进列表，
    与计数/列表页同口径）；自己的私有文档带锁标。计数口径与列表一致，
    他人私有文档不计入（否则「分类 N 篇」点进去对不上，用户以为系统坏了）。
    """

    def setUp(self):
        self.series = KnowledgeCategory.objects.create(name="Django 学习", sort_order=5)
        self.basic = KnowledgeCategory.objects.create(
            name="基础篇", parent=self.series, sort_order=1)
        self.other_cat = KnowledgeCategory.objects.create(
            name="其他篇", parent=self.series, sort_order=2)
        self.empty_cat = KnowledgeCategory.objects.create(name="空分类", sort_order=9)
        self.public_doc = KnowledgeDocument.objects.create(
            title="公开教程", category=self.basic, content="公开内容",
            status="published", visibility="public")
        KnowledgeDocument.objects.create(
            title="进阶教程", category=self.other_cat, content="进阶内容",
            status="published", visibility="public")
        self.owner = User.objects.create_user("kbowner", "owner@e.com", PWD, is_active=True)
        self.stranger = User.objects.create_user("kbstrange", "s@e.com", PWD, is_active=True)
        self.third = User.objects.create_user("kbthird", "t@e.com", PWD, is_active=True)
        grant(self.owner, ("knowledge", "read"))
        grant(self.stranger, ("knowledge", "read"))
        grant(self.third, ("knowledge", "read"))
        self.own_private = KnowledgeDocument.objects.create(
            title="我的私有笔记", category=self.basic, content="私有内容",
            status="published", visibility="private", owner=self.owner)
        self.other_private = KnowledgeDocument.objects.create(
            title="他人的私有笔记", category=self.basic, content="他人私有",
            status="published", visibility="private", owner=self.third)

    def tearDown(self):
        cache.clear()

    def _detail_html(self, user, doc):
        c = Client()
        c.force_login(user)
        return c.get(
            reverse("knowledge_base:document_detail", args=[doc.pk])).content.decode("utf-8")

    def _sidebar_html(self, user, doc):
        """详情页右栏「分类筛选」树片段（属性锚点避开 <style> 里的选择器，
        以「标签云」标题为下界——树区在统计区与标签云之间）。"""
        html = self._detail_html(user, doc)
        start = html.find('class="kb-cat-tree"')
        self.assertGreaterEqual(start, 0, "详情页右栏缺少分类筛选树")
        end = html.find("标签云", start)
        self.assertGreater(end, start, "分类树之后缺少标签云分区")
        return html[start:end]

    def test_right_sidebar_renders_on_detail_page(self):
        """详情页也有右栏：分类树（含分类管理按钮）+ 统计四联。"""
        html = self._detail_html(self.owner, self.public_doc)
        self.assertIn("kb-side-head", html)     # 管理/新建按钮并入分类筛选标题行
        self.assertIn("kb-stats-grid", html)    # 统计四联（单行压缩版）
        seg = self._sidebar_html(self.owner, self.public_doc)
        self.assertIn("Django 学习", seg)
        self.assertIn("基础篇", seg)
        self.assertIn("kb-cat-caret", seg)      # caret 折叠/展开 = 文件夹能力

    def test_every_category_with_content_gets_caret(self):
        """问题 3（2026-10-22）：每个有内容的分类都有 caret——有子分类的
           （Django 学习）、只有文档的（基础篇/其他篇）都给折叠/展开；
           两者皆空的分类才用 spacer（无可折叠内容，给了也是死控件）。"""
        seg = self._sidebar_html(self.owner, self.public_doc)
        self.assertIn('aria-label="展开或折叠「Django 学习」"', seg)  # 有子分类
        self.assertIn('aria-label="展开或折叠「基础篇」"', seg)      # 只有文档
        self.assertIn('aria-label="展开或折叠「其他篇」"', seg)      # 只有文档
        self.assertIn("kb-cat-caret kb-cat-caret-spacer", seg)      # 空分类

    def test_expanded_area_lists_docs_and_subcategories(self):
        """caret 展开区 = 文档 + 子分类树（文件夹系统的等价物）；
           详情页当前文档行高亮，自己的私有文档带锁标。"""
        seg = self._sidebar_html(self.owner, self.public_doc)
        self.assertIn("kb-cat-doc-title", seg)        # 文档行
        self.assertIn("公开教程", seg)                 # 基础篇的公开文档
        self.assertIn("进阶教程", seg)                 # 其他篇的文档
        self.assertIn("kb-cat-doc is-current", seg)   # 当前文档高亮
        self.assertIn('aria-current="page"', seg)
        self.assertIn("kb-cat-doc-lock", seg)         # 自己的私有带锁标
        self.assertIn("我的私有笔记", seg)

    def test_current_category_highlighted_and_ancestors_expanded(self):
        """当前文档所在分类高亮（active），其祖先链默认展开（is-open、展开区
           无 hidden）；不在链上的分类保持折叠（其他篇）。"""
        seg = self._sidebar_html(self.owner, self.public_doc)
        self.assertIn(
            '?category=%d" class="kb-side-list-item kb-cat-link active' % self.basic.pk,
            seg, "当前文档所在分类未高亮")
        self.assertIn("is-open", seg)       # 链上 caret 展开态
        for cat in (self.series, self.basic):
            node_html = seg[seg.find('?category=%d"' % cat.pk):]
            container = node_html[node_html.find("kb-cat-children"):]
            self.assertTrue(
                container.startswith('kb-cat-children">'),
                f"「{cat.name}」的展开区被折叠了")
        self.assertIn('kb-cat-children" hidden', seg)   # 链外分类保持折叠

    def test_category_count_uses_visibility_scope(self):
        """计数口径与列表一致：owner 计入自己的私有（2），陌生人只看公开（1）。"""
        seg = self._sidebar_html(self.owner, self.public_doc)
        self.assertIn(
            '<span>基础篇</span><span class="kb-side-list-count">2</span>', seg)
        seg = self._sidebar_html(self.stranger, self.public_doc)
        self.assertIn(
            '<span>基础篇</span><span class="kb-side-list-count">1</span>', seg)

    def test_private_doc_titles_never_leak(self):
        """铁律：树里的文档只有当前用户可见的——他人私有标题绝不出现，
           自己的私有文档才出现（且带锁标）。"""
        owner_seg = self._sidebar_html(self.owner, self.public_doc)
        self.assertNotIn("他人的私有笔记", owner_seg)
        self.assertIn("我的私有笔记", owner_seg)
        stranger_seg = self._sidebar_html(self.stranger, self.public_doc)
        self.assertNotIn("他人的私有笔记", stranger_seg)
        self.assertNotIn("我的私有笔记", stranger_seg)
        self.assertIn("公开教程", stranger_seg)


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
        self.assertIn("树下文档", html)        # 展开区里的文档行（2026-10-22）
        self.assertIn("kb-cat-doc-title", html)

    def test_caret_toggle_script_present(self):
        """caret 折叠脚本在基座（列表页与详情页共用，不得只放列表页）。"""
        html = self.client.get(reverse("knowledge_base:document_list")).content.decode("utf-8")
        self.assertIn("kb-cat-caret:not(.kb-cat-caret-spacer)", html)

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
