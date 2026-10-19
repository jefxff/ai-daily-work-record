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

    class Meta:
        verbose_name = '知识文档'
        verbose_name_plural = '知识文档'
        ordering = ['-updated_at']

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
