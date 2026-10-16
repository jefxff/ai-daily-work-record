from django.conf import settings
from django.db import models


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
    complaint_status = models.CharField(max_length=20, verbose_name='客诉状态', null=False, blank=False, choices=[('未完成', '未完成'), ('处理中', '处理中'), ('已完成', '已完成')])
    follow_up_status = models.TextField(verbose_name='跟进情况', null=True, blank=True)
    remarks = models.TextField(verbose_name='备注', null=True, blank=True)
    
    class Meta:
        verbose_name = '客户客诉'
        verbose_name_plural = '客户客诉'
        ordering = ['-id']


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
    department = models.CharField(max_length=50, verbose_name='提出部门', null=False, blank=False)
    work_content = models.TextField(verbose_name='工作内容', null=False, blank=False)
    due_date = models.DateField(verbose_name='截至日期', null=True, blank=True)
    status = models.CharField(max_length=10, verbose_name='工作状态', null=False, blank=False, choices=[('完成', '完成'), ('未完成', '未完成')])
    progress = models.IntegerField(verbose_name='工作进度', null=False, blank=False, default=0, help_text='0-100')

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
        
    def __str__(self):
        return f"{self.metal_name} - {self.price} {self.unit} ({self.crawl_date})"


class Insurance(models.Model):
    """保险模型"""
    company_name = models.CharField(max_length=100, verbose_name='公司名称', null=False, blank=False)
    title = models.CharField(max_length=200, verbose_name='标题', null=False, blank=False)
    order_number = models.CharField(max_length=100, verbose_name='单号', null=True, blank=True)
    premium = models.DecimalField(max_digits=12, decimal_places=2, verbose_name='保费', null=False, blank=False)
    issue_date = models.DateField(verbose_name='签发日期', null=False, blank=False)
    end_date = models.DateField(verbose_name='截止日期', null=False, blank=False)
    department = models.CharField(max_length=50, verbose_name='部门', null=False, blank=False)
    remarks = models.TextField(verbose_name='备注', null=True, blank=True)
    
    @property
    def is_expired(self):
        """判断是否到期"""
        from datetime import date
        today = date.today()
        if self.end_date < today:
            return True
        return False
    
    @property
    def expired_days(self):
        """计算到期天数"""
        from datetime import date
        today = date.today()
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
    dispute_type = models.CharField(max_length=100, verbose_name='纠纷类型', null=False, blank=False)
    defendant = models.CharField(max_length=100, verbose_name='被告', null=False, blank=False)
    case_number = models.CharField(max_length=50, verbose_name='案号', null=False, blank=False)
    amount = models.DecimalField(max_digits=10, decimal_places=2, verbose_name='涉案金额（元）', null=False, blank=False)
    court = models.CharField(max_length=100, verbose_name='受理法院', null=False, blank=False)
    lawyer = models.CharField(max_length=50, verbose_name='代理律师', null=False, blank=False)
    progress = models.CharField(max_length=50, verbose_name='进度', null=False, blank=False, choices=[('立案', '立案'), ('审理中', '审理中'), ('判决', '判决'), ('执行', '执行'), ('结案', '结案')])
    detailed_progress = models.TextField(verbose_name='详细进度', null=True, blank=True)
    remarks = models.TextField(verbose_name='备注', null=True, blank=True)
    
    class Meta:
        verbose_name = '客诉'
        verbose_name_plural = '客诉'
        ordering = ['-id']
    
    def __str__(self):
        return f"{self.case_number} - {self.defendant}"


class OperationLog(models.Model):
    """操作留痕模型"""
    operation_time = models.DateTimeField(auto_now_add=True, verbose_name='操作时间')
    operator = models.CharField(max_length=100, verbose_name='操作人')
    module_type = models.CharField(max_length=50, verbose_name='模块类型')
    operation_type = models.CharField(max_length=20, verbose_name='操作类型')
    object_identifier = models.CharField(max_length=200, verbose_name='对象标识')
    change_summary = models.TextField(verbose_name='变更摘要', null=True, blank=True)

    class Meta:
        verbose_name = '操作留痕'
        verbose_name_plural = '操作留痕'
        ordering = ['-operation_time']
