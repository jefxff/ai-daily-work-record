from datetime import datetime, time, timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone


class Decoration(models.Model):
    """装修记录模型"""
    project_name = models.CharField(max_length=100, verbose_name='项目名称', null=False, blank=False)
    constructor = models.CharField(max_length=100, verbose_name='施工方', null=False, blank=False)
    project_address = models.CharField(max_length=200, verbose_name='项目地址', null=True, blank=True)
    start_date = models.DateField(verbose_name='开工日期', null=False, blank=False)
    original_price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name='原价', null=True, blank=True)
    final_price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name='核后价', null=False, blank=False)
    reduced_price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name='核减金额', null=True, blank=True)
    
    def save(self, *args, **kwargs):
        # 自动计算核减金额
        if self.original_price and self.final_price:
            self.reduced_price = self.original_price - self.final_price
        super().save(*args, **kwargs)
    
    class Meta:
        verbose_name = '装修记录'
        verbose_name_plural = '装修记录'
        ordering = ['-id']


class CustomerCredit(models.Model):
    """客户账期模型"""
    proposal_date = models.DateField(verbose_name='提出日期', null=False, blank=False)
    site_date = models.DateField(verbose_name='现场日期', null=True, blank=True)
    customer_name = models.CharField(max_length=100, verbose_name='客户名称', null=False, blank=False)
    customer_type = models.CharField(max_length=50, verbose_name='客户类型', null=False, blank=False, default='普通客户', choices=[('普通客户', '普通客户'), ('代加工客户', '代加工客户')])
    salesperson = models.CharField(max_length=50, verbose_name='业务员', null=False, blank=False)
    work_content = models.CharField(max_length=50, verbose_name='工作内容', null=True, blank=True, choices=[('客户催款或复审', '客户催款或复审'), ('客户账期审核', '客户账期审核'), ('提升账期或额度', '提升账期或额度')])
    specific_content = models.CharField(max_length=50, verbose_name='具体内容', null=True, blank=True, choices=[('老客户重审', '老客户重审'), ('提升账期或额度', '提升账期或额度'), ('新客户账期', '新客户账期')])
    pre_survey = models.CharField(max_length=10, verbose_name='前期调查表', null=True, blank=True, choices=[('有', '有'), ('无', '无')])
    work_method = models.CharField(max_length=20, verbose_name='工作方式', null=True, blank=True, choices=[('刘总特批', '刘总特批'), ('现场审核', '现场审核'), ('内部流程', '内部流程')])
    result = models.CharField(max_length=100, verbose_name='结果', null=False, blank=False)
    remarks = models.TextField(max_length=500, verbose_name='备注', null=True, blank=True)
    has_assessment_period = models.BooleanField(default=False, verbose_name='是否有业绩考核期')
    assessment_start_date = models.DateField(verbose_name='考核期开始日期', null=True, blank=True)
    assessment_end_date = models.DateField(verbose_name='考核期结束日期', null=True, blank=True)
    assessment_content = models.CharField(max_length=500, verbose_name='考核内容', null=True, blank=True, default='')

    class Meta:
        verbose_name = '客户账期'
        verbose_name_plural = '客户账期'
        ordering = ['-id']


class CustomerComplaint(models.Model):
    """客户客诉模型"""
    customer_name = models.CharField(max_length=100, verbose_name='客户名称', null=False, blank=False)
    responsible_salesperson = models.CharField(max_length=50, verbose_name='责任业务员', null=False, blank=False)
    complaint_date = models.DateField(verbose_name='客诉申请日期', null=False, blank=False)
    terminal_customer = models.CharField(max_length=100, verbose_name='终端客户', null=True, blank=True)
    product_model = models.CharField(max_length=50, verbose_name='产品型号', null=True, blank=True)
    our_shipment = models.IntegerField(verbose_name='我司出货量', null=True, blank=True)
    customer_shipment = models.IntegerField(verbose_name='客户出货量', null=True, blank=True)
    complaint_description = models.TextField(verbose_name='客诉现象及客户诉求描述', null=True, blank=True)
    compensation_amount = models.DecimalField(max_digits=10, decimal_places=2, verbose_name='客户诉求（赔偿金额）', null=True, blank=True)
    is_insured = models.BooleanField(verbose_name='是否报保险', null=True, blank=True)
    engineering_report = models.TextField(verbose_name='工程报告', null=True, blank=True)
    engineering_analysis = models.TextField(verbose_name='工程部分析原因报告', null=True, blank=True)
    missing_materials = models.CharField(max_length=200, verbose_name='欠缺材料', null=True, blank=True)
    complaint_status = models.CharField(max_length=20, verbose_name='客诉状态', null=False, blank=False, default='未完成', choices=[('未完成', '未完成'), ('处理中', '处理中'), ('已完成', '已完成')])
    follow_up_status = models.TextField(verbose_name='跟进情况', null=True, blank=True)
    # 跟进阶段（结构化统计用）：按关键词从 follow_up_status 归一，保留原始自由文本
    LEGAL_STAGE_CHOICES = [
        ('', '无'),
        ('内部处理', '内部处理'),
        ('保险理赔', '保险理赔'),
        ('协商中', '协商中'),
        ('准备诉讼', '准备诉讼'),
        ('诉讼中', '诉讼中'),
        ('已判决', '已判决'),
    ]
    legal_stage = models.CharField(
        max_length=20, blank=True, default='', verbose_name='跟进阶段',
        choices=LEGAL_STAGE_CHOICES,
    )
    remarks = models.TextField(verbose_name='备注', null=True, blank=True)

    # —— 闭环字段群（P2 客诉闭环）——
    closed_at = models.DateTimeField(verbose_name='闭环时间', null=True, blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name='闭环人',
        null=True, blank=True, on_delete=models.SET_NULL,
        related_name='closed_complaints',
    )
    closure_result = models.CharField(
        verbose_name='闭环结论', max_length=20, blank=True,
        choices=[('accepted', '客户接受'), ('partial', '部分接受'), ('rejected', '不接受')],
    )
    closure_note = models.TextField(verbose_name='闭环说明', blank=True)
    # SLA 承诺时限（小时），0 表示不考核
    sla_hours = models.PositiveIntegerField(verbose_name='SLA时限(小时)', default=0, help_text='0 表示不考核')

    def clean(self):
        """客诉状态必须由闭环动作推导，禁止在编辑表单或 API 中直接改为「已完成」。"""
        from django.core.exceptions import ValidationError
        if self.complaint_status == '已完成' and not self.closed_at:
            raise ValidationError({
                'complaint_status': '客诉状态不能直接改为「已完成」，请通过「闭环」操作完成',
            })

    class Meta:
        verbose_name = '客户客诉'
        verbose_name_plural = '客户客诉'
        ordering = ['-id']

    # —— 闭环 / SLA 便捷属性 ——
    @property
    def is_closed(self):
        return self.closed_at is not None

    @property
    def sla_deadline(self):
        """SLA 截止时间 = 客诉申请日期 00:00 + sla_hours 小时。"""
        if not self.complaint_date or not self.sla_hours:
            return None
        start = datetime.combine(self.complaint_date, time.min)
        if settings.USE_TZ:
            start = timezone.make_aware(start)
        return start + timedelta(hours=self.sla_hours)

    @property
    def sla_remaining(self):
        """距 SLA 截止的剩余 timedelta，已超时为负。"""
        deadline = self.sla_deadline
        if deadline is None:
            return None
        return deadline - timezone.now()

    @property
    def is_sla_overdue(self):
        if self.is_closed:
            return False
        remaining = self.sla_remaining
        return remaining is not None and remaining.total_seconds() < 0

    @property
    def sla_overdue_days(self):
        """超时天数（正数）；未超时或无截止时间返回 0。"""
        remaining = self.sla_remaining
        if self.is_closed or remaining is None:
            return 0
        seconds = remaining.total_seconds()
        if seconds >= 0:
            return 0
        return int((-seconds + 86399) // 86400)

    @property
    def closure_result_display(self):
        return dict(self._meta.get_field('closure_result').flatchoices).get(self.closure_result, '')

    @property
    def closure_timeline_steps(self):
        """闭环五步法时间线：受理登记 → 核查定责 → 处置方案 → 回复客户 → 闭环确认。

        仅「受理登记」与「闭环确认」有明确日期（complaint_date / closed_at），
        中间三步为流程占位，闭环前显示为 todo，闭环后显示为 done。
        """
        closed = self.is_closed
        steps = [
            ('受理登记', self.complaint_date.strftime('%Y-%m-%d') if self.complaint_date else '', 'done'),
            ('核查定责', '', 'done' if closed else 'todo'),
            ('处置方案', '', 'done' if closed else 'todo'),
            ('回复客户', '', 'done' if closed else 'todo'),
            (
                '闭环确认',
                timezone.localtime(self.closed_at).strftime('%Y-%m-%d %H:%M') if closed else '待操作',
                'done' if closed else 'current',
            ),
        ]
        return steps


class Regulation(models.Model):
    """制度及处罚模型"""
    issue_date = models.DateField(verbose_name='出文日期', null=False, blank=False)
    title = models.CharField(max_length=200, verbose_name='标题', null=False, blank=False)
    content = models.TextField(verbose_name='主要内容', null=False, blank=False)
    doc_no = models.CharField(max_length=200, verbose_name='文件编号', null=False, blank=False)
    
    class Meta:
        verbose_name = '制度及处罚'
        verbose_name_plural = '制度及处罚'
        ordering = ['-id']


class PriceEvaluation(models.Model):
    """核价模型"""
    project_category = models.CharField(max_length=50, verbose_name='项目分类', null=False, blank=False)
    proposing_department = models.CharField(max_length=50, verbose_name='项目提出部门', null=False, blank=False)
    proposer = models.CharField(max_length=50, verbose_name='提出人', null=False, blank=False)
    evaluation_content = models.TextField(verbose_name='核价内容', null=True, blank=True)
    supplier1_name = models.CharField(max_length=50, verbose_name='供应商1名称', null=True, blank=True)
    supplier1_price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name='供应商1报价', null=True, blank=True)
    supplier2_name = models.CharField(max_length=50, verbose_name='供应商2名称', null=True, blank=True)
    supplier2_price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name='供应商2报价', null=True, blank=True)
    supplier3_name = models.CharField(max_length=50, verbose_name='供应商3名称', null=True, blank=True)
    supplier3_price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name='供应商3报价', null=True, blank=True)
    internal_control_supplier = models.CharField(max_length=50, verbose_name='内控审核供应商', null=True, blank=True)
    internal_control_price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name='内控审核价', null=False, blank=False)
    
    def get_reduction_amount(self):
        """计算核减金额（取最高供应商报价与内控审核价的差额）"""
        prices = [p for p in [self.supplier1_price, self.supplier2_price, self.supplier3_price] if p is not None]
        if prices and self.internal_control_price:
            max_price = max(prices)
            return max_price - self.internal_control_price
        return 0
    
    class Meta:
        verbose_name = '核价'
        verbose_name_plural = '核价'
        ordering = ['-id']


class DailyRecord(models.Model):
    """每日记录模型"""
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='daily_records',
        verbose_name='归属用户',
        null=False,
        blank=False,
        help_text='记录归属用户，查询/操作均按此字段隔离',
    )
    record_date = models.DateTimeField(verbose_name='日期及时间', null=False, blank=False, auto_now_add=True)
    department = models.CharField(max_length=50, verbose_name='提出部门/提出人', null=False, blank=False)
    work_content = models.TextField(verbose_name='工作内容', null=False, blank=False)
    due_date = models.DateField(verbose_name='截至日期', null=True, blank=True)
    status = models.CharField(max_length=10, verbose_name='工作状态', null=False, blank=False, choices=[('完成', '完成'), ('未完成', '未完成')])
    progress = models.IntegerField(verbose_name='工作进度', null=False, blank=False, default=0, help_text='0-100')

    # —— 知识库沉淀（批次 3）：两问一起问，否则没法表达「沉淀，但只给自己看」——
    kb_enabled = models.BooleanField('沉淀到知识库', default=False)
    kb_visibility = models.CharField(
        '沉淀范围', max_length=10, default='private',
        choices=[('private', '仅自己可见'), ('public', '全员可见')],
        help_text='仅影响知识库里的可见范围，每日记录本身始终只有本人可见')
    kb_doc = models.ForeignKey(
        'knowledge_base.KnowledgeDocument', verbose_name='沉淀文档',
        null=True, blank=True, on_delete=models.SET_NULL,
        related_name='daily_records',
        help_text='沉淀到知识库后的大纲节点；文档被删时置空（节点已随文档级联删除）')
    kb_synced_at = models.DateTimeField('沉淀时间', null=True, blank=True)

    class Meta:
        verbose_name = '每日记录'
        verbose_name_plural = '每日记录'
        ordering = ['-record_date']
        indexes = [
            models.Index(fields=['user', '-record_date'], name='idx_dailyrec_user_date'),
        ]


class MetalPrice(models.Model):
    """金属价格模型"""
    metal_name = models.CharField(max_length=50, verbose_name='金属名称', null=False, blank=False)
    price = models.DecimalField(max_digits=12, decimal_places=2, verbose_name='价格', null=False, blank=False)
    unit = models.CharField(max_length=20, verbose_name='单位', null=False, blank=False)
    price_change = models.DecimalField(max_digits=12, decimal_places=2, verbose_name='涨跌', null=True, blank=True)
    crawl_date = models.DateField(verbose_name='爬取日期', null=False, blank=False, auto_now_add=True)
    crawl_time = models.DateTimeField(verbose_name='爬取时间', null=False, blank=False, auto_now_add=True)
    
    class Meta:
        verbose_name = '金属价格'
        verbose_name_plural = '金属价格'
        ordering = ['-crawl_time']
        # 首页/看板每条请求都按 crawl_date 取当日或最近一批（审计 #20）
        indexes = [models.Index(fields=['crawl_date'], name='idx_metalprice_crawl_date')]
        
    def __str__(self):
        return f"{self.metal_name} - {self.price} {self.unit} ({self.crawl_date})"


class InsuranceSubject(models.Model):
    """保险标的主档：一台车（车牌）或一个非车险险种（阶段 2 落库）。

    归一口径与 records.insurance_grouping.subject_of 完全一致：
    - 车险：plate 唯一（如 粤B8X66D），insurance_type 固定为「车险」；
    - 非车险：(kind, insurance_type) 唯一（如 财产一切险）。
    """

    KIND_CHOICES = [('car', '车险'), ('non_car', '非车险')]

    kind = models.CharField(max_length=20, choices=KIND_CHOICES, verbose_name='标的类型')
    label = models.CharField(max_length=100, verbose_name='标的名称')
    plate = models.CharField(max_length=20, blank=True, default='', verbose_name='车牌号')
    insurance_type = models.CharField(max_length=100, verbose_name='险种')
    department = models.CharField(max_length=50, blank=True, default='', verbose_name='默认部门')
    remarks = models.TextField(null=True, blank=True, verbose_name='备注')
    is_one_off = models.BooleanField(
        default=False, verbose_name='一次性保险',
        help_text='旅游意外、诉前保全、工程施工等不续保的标的，勾选后不再提醒续保',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '保险标的'
        verbose_name_plural = '保险标的'
        constraints = [
            models.UniqueConstraint(
                fields=['plate'], condition=~models.Q(plate=''),
                name='uniq_inssubject_plate',
            ),
            models.UniqueConstraint(
                fields=['kind', 'insurance_type'], condition=models.Q(kind='non_car'),
                name='uniq_inssubject_noncar_type',
            ),
        ]

    def __str__(self):
        return self.label


class InsurancePolicyYear(models.Model):
    """保单年度：标的一个截止年份下的全部保单条目汇总（冗余字段由明细重算）。"""

    subject = models.ForeignKey(
        InsuranceSubject, on_delete=models.CASCADE,
        related_name='policy_years', verbose_name='保险标的',
    )
    year = models.PositiveIntegerField(verbose_name='保单年度')
    start_date = models.DateField(verbose_name='保障开始')
    end_date = models.DateField(verbose_name='保障截止')
    premium = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name='保费合计')
    insurer = models.CharField(max_length=100, blank=True, default='', verbose_name='保司（归一）')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '保单年度'
        verbose_name_plural = '保单年度'
        ordering = ['subject_id', 'year']
        constraints = [
            models.UniqueConstraint(
                fields=['subject', 'year'], name='uniq_inspolicyyear_subject_year',
            ),
        ]

    def __str__(self):
        return f'{self.subject.label} {self.year} 年度'


class Insurance(models.Model):
    """保险模型"""
    company_name = models.CharField(max_length=100, verbose_name='公司名称', null=False, blank=False)
    # 保司归一名称（由 insurance_grouping.normalize_insurer 回填，保留 company_name 原始值）
    canonical_name = models.CharField(max_length=100, blank=True, default='', verbose_name='保司（归一）')
    title = models.CharField(max_length=200, verbose_name='标题', null=False, blank=False)
    order_number = models.CharField(max_length=100, verbose_name='单号', null=True, blank=True)
    premium = models.DecimalField(max_digits=12, decimal_places=2, verbose_name='保费', null=False, blank=False)
    issue_date = models.DateField(verbose_name='签发日期', null=False, blank=False)
    end_date = models.DateField(verbose_name='截止日期', null=False, blank=False)
    department = models.CharField(max_length=50, verbose_name='部门', null=False, blank=False)
    remarks = models.TextField(verbose_name='备注', null=True, blank=True)
    # 阶段 2：标的/年度挂接（可空，由 insurance_services.attach_record 自动维护）
    subject = models.ForeignKey(
        InsuranceSubject, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='records', verbose_name='保险标的',
    )
    policy_year = models.ForeignKey(
        InsurancePolicyYear, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='records', verbose_name='保单年度',
    )
    
    @property
    def is_expired(self):
        """判断是否到期"""
        today = timezone.localdate()
        if self.end_date < today:
            return True
        return False
    
    @property
    def expired_days(self):
        """计算到期天数"""
        today = timezone.localdate()
        if self.end_date < today:
            days = (today - self.end_date).days
            return f"已到期{days}天"
        else:
            days = (self.end_date - today).days
            return f"还有{days}天到期"
    
    class Meta:
        verbose_name = '保险'
        verbose_name_plural = '保险'
        ordering = ['-id']


class Complaint(models.Model):
    """客诉模型"""
    # 诉讼五态（保持现有 choices 不变，避免存量数据迁移）
    PROGRESS_CHOICES = [('立案', '立案'), ('审理中', '审理中'), ('判决', '判决'), ('执行', '执行'), ('结案', '结案')]
    # 状态 → 对应日期字段名（状态推进时由视图自动回填，未覆盖到的状态为 None）
    PROGRESS_DATE_FIELD = {
        '立案': 'filed_at',
        '审理中': 'hearing_at',
        '判决': 'judgment_at',
        '执行': 'execution_at',
        '结案': 'closed_at',
    }

    dispute_type = models.CharField(max_length=100, verbose_name='纠纷类型', null=False, blank=False)
    defendant = models.CharField(max_length=100, verbose_name='被告', null=False, blank=False)
    case_number = models.CharField(max_length=50, verbose_name='案号', null=False, blank=False)
    amount = models.DecimalField(max_digits=10, decimal_places=2, verbose_name='涉案金额（元）', null=False, blank=False)
    court = models.CharField(max_length=100, verbose_name='受理法院', null=False, blank=False)
    lawyer = models.CharField(max_length=50, verbose_name='代理律师', null=False, blank=False)
    progress = models.CharField(max_length=50, verbose_name='进度', null=False, blank=False, choices=PROGRESS_CHOICES)
    detailed_progress = models.TextField(verbose_name='详细进度', null=True, blank=True)
    remarks = models.TextField(verbose_name='备注', null=True, blank=True)

    # —— 五态关键日期（P3 诉讼时间线）：状态推进时自动回填 ——
    filed_at = models.DateField(verbose_name='立案日期', null=True, blank=True)
    hearing_at = models.DateField(verbose_name='开庭日期', null=True, blank=True)
    judgment_at = models.DateField(verbose_name='判决日期', null=True, blank=True)
    execution_at = models.DateField(verbose_name='执行日期', null=True, blank=True)
    closed_at = models.DateField(verbose_name='结案日期', null=True, blank=True)

    class Meta:
        verbose_name = '客诉'
        verbose_name_plural = '客诉'
        ordering = ['-id']

    def __str__(self):
        return f"{self.case_number} - {self.defendant}"

    # —— 诉讼时间线便捷属性 ——
    @property
    def answer_deadline(self):
        """答辩期截止 = 立案日期 + 15 天。"""
        return self.filed_at + timedelta(days=15) if self.filed_at else None

    @property
    def progress_date(self):
        """当前状态对应的日期字段值。"""
        field = self.PROGRESS_DATE_FIELD.get(self.progress)
        return getattr(self, field) if field else None

    @property
    def current_progress_days(self):
        """当前状态已持续天数（自该状态日期起算）。"""
        d = self.progress_date
        if not d:
            return None
        return (timezone.localdate() - d).days

    @property
    def timeline_steps(self):
        """供模板渲染的五态时间线数据：[(状态, 日期, 阶段), ...]。

        阶段：done=已完成, current=当前, todo=未到达。以 progress 在 choices 中的
        位置为分界点。
        """
        order = [c[0] for c in self.PROGRESS_CHOICES]
        try:
            current_idx = order.index(self.progress)
        except ValueError:
            current_idx = 0
        steps = []
        for idx, status in enumerate(order):
            field = self.PROGRESS_DATE_FIELD.get(status)
            d = getattr(self, field) if field else None
            if idx < current_idx:
                stage = 'done'
            elif idx == current_idx:
                stage = 'current'
            else:
                stage = 'todo'
            steps.append((status, d, stage))
        return steps


class OperationLog(models.Model):
    """操作留痕模型"""
    operation_time = models.DateTimeField(auto_now_add=True, verbose_name='操作时间')
    operator = models.CharField(max_length=100, verbose_name='操作人')
    # 操作人账号（用于聚合与追责）：operator 是**展示名**，可能记的是姓名，
    # 同一个人的操作会因入口不同被记成「张三」和「zhangsan」两条
    # （一致性审计 P0-2）。账号列不参与展示，只用于查询/统计分组。
    # 空值表示历史行或非登录来源（如 API Token 前的匿名 IP），聚合时回退到 operator。
    operator_username = models.CharField(
        max_length=150, blank=True, default="", db_index=True,
        verbose_name='操作人账号',
    )
    module_type = models.CharField(max_length=50, verbose_name='模块类型')
    operation_type = models.CharField(max_length=20, verbose_name='操作类型')
    object_identifier = models.CharField(max_length=200, verbose_name='对象标识')
    change_summary = models.TextField(verbose_name='变更摘要', null=True, blank=True)
    # 变更前/后字段快照（records.audit.snapshot 归一后的 JSON 安全字典）：
    # 新增仅 after、删除仅 before、编辑两者皆有；导出等无实体变更的动作为空。
    before_json = models.JSONField(verbose_name='变更前', null=True, blank=True)
    after_json = models.JSONField(verbose_name='变更后', null=True, blank=True)

    # 单条留痕最多展示的字段级变更项，避免大对象撑爆列表页
    MAX_DIFF_ITEMS = 50

    @property
    def change_diff(self):
        """字段级变更明细：[(字段名, 变更前, 变更后), ...]，仅含前后不一致的字段。

        新增（无 before）与删除（无 after）时，缺失的一侧为 None。
        无快照（导出等动作）返回空列表。
        """
        before = self.before_json or {}
        after = self.after_json or {}
        if not before and not after:
            return []
        diff = []
        for key in dict.fromkeys(list(before) + list(after)):
            old, new = before.get(key), after.get(key)
            if old != new:
                diff.append((key, old, new))
                if len(diff) >= self.MAX_DIFF_ITEMS:
                    break
        return diff

    class Meta:
        verbose_name = '操作留痕'
        verbose_name_plural = '操作留痕'
        ordering = ['-operation_time']
        # 留痕列表按时间倒序分页、且写入量在补齐创建留痕后上升（审计 #20 / #8）
        indexes = [
            models.Index(fields=['operation_time'], name='idx_oplog_time'),
            models.Index(fields=['module_type', 'operation_time'], name='idx_oplog_module_time'),
        ]
