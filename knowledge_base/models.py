from django.conf import settings
from django.db import models


class KnowledgeCategory(models.Model):
    """知识分类"""
    name = models.CharField(max_length=100, verbose_name='分类名称')
    parent = models.ForeignKey(
        'self',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='children',
        verbose_name='上级分类'
    )
    description = models.TextField(max_length=500, null=True, blank=True, verbose_name='分类描述')
    sort_order = models.IntegerField(default=0, verbose_name='排序')

    class Meta:
        verbose_name = '知识分类'
        verbose_name_plural = '知识分类'
        ordering = ['sort_order', 'name']

    def __str__(self):
        return self.name


class KnowledgeDocument(models.Model):
    """知识文档"""
    STATUS_CHOICES = [
        ('draft', '草稿'),
        ('published', '已发布'),
    ]

    title = models.CharField(max_length=200, verbose_name='文档标题')
    category = models.ForeignKey(
        KnowledgeCategory,
        on_delete=models.SET_NULL,
        null=True,
        related_name='documents',
        verbose_name='所属分类'
    )
    content = models.TextField(verbose_name='文档内容')
    summary = models.TextField(max_length=500, null=True, blank=True, verbose_name='摘要')
    tags = models.CharField(max_length=200, null=True, blank=True, verbose_name='标签', help_text='多个标签用逗号分隔')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='published', verbose_name='状态')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')
    view_count = models.IntegerField(default=0, verbose_name='浏览次数')

    # —— 可见性（批次 1）：默认私有 + 主动公开 + 可随时收回，规则见 access.py ——
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name='创建人', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='knowledge_docs')
    visibility = models.CharField(
        '可见性', max_length=10, default='private', db_index=True,
        choices=[('private', '仅自己可见'), ('public', '全员可见')])
    # 来源标记：沉淀容器等系统写入的文档带固定指纹，定位/清理以本字段为准，
    # 不依赖标题字符串（用户改标题不影响同步逻辑）
    source = models.CharField('来源标记', max_length=32, blank=True, default='')

    # —— 幕布化（批次 3 模型层 / 批次 4 编辑器）：——
    editor_mode = models.CharField(
        '编辑模式', max_length=10, default='markdown',
        choices=[('markdown', 'Markdown'), ('outline', '大纲')],
        help_text='首次在编辑器中保存后转为 outline')
    tree_version = models.PositiveIntegerField('树版本(乐观锁)', default=1)

    class Meta:
        verbose_name = '知识文档'
        verbose_name_plural = '知识文档'
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['owner', 'visibility'], name='idx_kdoc_owner_vis'),
        ]

    def __str__(self):
        return self.title

    @property
    def tag_list(self):
        if self.tags:
            return [tag.strip() for tag in self.tags.split(',') if tag.strip()]
        return []


class KnowledgeView(models.Model):
    """知识文档阅读记录：每次打开详情页写一行。

    与 ``KnowledgeDocument.view_count``（仅累计总次数）互补：本表保留
    阅读人 / 阅读时间 / 来源 IP 的完整轨迹，供「阅读报告」按人、按文档、按时间聚合。
    用户删除后轨迹不丢（user 用 SET_NULL，viewer 存用户名快照便于事后追溯）。
    """

    document = models.ForeignKey(
        KnowledgeDocument, on_delete=models.CASCADE,
        related_name='view_records', verbose_name='文档',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='knowledge_view_records',
        verbose_name='阅读用户',
    )
    viewer = models.CharField('阅读人', max_length=100, blank=True, default='')
    ip = models.CharField('来源IP', max_length=64, blank=True, default='')
    viewed_at = models.DateTimeField('阅读时间', auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = '知识阅读记录'
        verbose_name_plural = verbose_name
        ordering = ['-viewed_at']
        indexes = [
            models.Index(fields=['document', '-viewed_at'], name='idx_kview_doc_time'),
            models.Index(fields=['user', '-viewed_at'], name='idx_kview_user_time'),
        ]

    def __str__(self):
        return f"{self.viewer or '匿名'} → 文档#{self.document_id}"


class KnowledgeNode(models.Model):
    """大纲节点：邻接表 + 同级 sort（幕布化核心表）。

    - 邻接表（parent 外键）而不是物化路径 / JSON 整块存储：一次查询取整棵树
      按 (parent_id, sort) 内存组装，几十毫秒内完成；整树保存时同父节点
      重排为 0..n，不存在小数精度耗尽问题。
    - uid 是前端生成的稳定 id（12 位），diff 用，和数据库自增 pk 无关。
    - ref_* 是业务引用三元组（与 accounts.registry.MODULES 的 key 对齐），
      不走 GenericForeignKey：权限按 ref_module 就能调 has_perm 过滤，
      渲染时也能按模块映射到详情路由。
    """

    TYPE = [
        ('text', '文本'), ('todo', '待办'), ('code', '代码'),
        ('quote', '引用块'), ('divider', '分隔'), ('ref', '业务引用'),
    ]

    document = models.ForeignKey(
        KnowledgeDocument, on_delete=models.CASCADE,
        related_name='nodes', verbose_name='文档')
    uid = models.CharField('前端节点ID', max_length=12, db_index=True)
    parent = models.ForeignKey(
        'self', on_delete=models.CASCADE, null=True, blank=True,
        related_name='children', verbose_name='父节点')
    sort = models.IntegerField('同级顺序', default=0)
    text = models.TextField('文本', blank=True)   # 行内标记：**粗体** `code` 等
    note = models.TextField('备注', blank=True)
    node_type = models.CharField(
        '节点类型', max_length=10, choices=TYPE, default='text')
    is_done = models.BooleanField('已完成', default=False)   # 仅 todo 有意义
    due_date = models.DateField('截至日期', null=True, blank=True)
    collapsed = models.BooleanField('已折叠', default=False)
    heading_level = models.SmallIntegerField('标题层级', null=True, blank=True)  # 1-3，用于目录

    ref_module = models.CharField('引用模块', max_length=32, blank=True, default='')
    ref_object_id = models.PositiveIntegerField('引用对象ID', null=True, blank=True)
    ref_label = models.CharField('引用名称快照', max_length=200, blank=True, default='')

    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = '知识大纲节点'
        verbose_name_plural = verbose_name
        ordering = ['sort', 'id']
        indexes = [
            models.Index(fields=['document', 'parent', 'sort']),
            models.Index(fields=['node_type', 'is_done', 'due_date']),  # 待办聚合
        ]

    def __str__(self):
        return f"节点#{self.pk}({self.node_type})@文档#{self.document_id}"
