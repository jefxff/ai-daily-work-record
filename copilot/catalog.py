"""模块目录：工具层能碰哪些模型、看哪些字段、按什么聚合。

这是"AI 能看到什么"的**唯一配置源**。新增模块接入 Copilot 只需在
``MODULES`` 追加一条，工具、字段字典、示例问题、引用链接都会自动跟进，
不需要改工具实现。

字段白名单是安全边界的一部分：未列出的字段（如 ``CustomerLedger.attachment``
的 FileField）模型永远看不到，避免把存储路径带进 prompt。
"""

from dataclasses import dataclass
from datetime import timedelta

from django.apps import apps
from django.db.models import Q
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

# 数据分级：L1 公开 / L2 内部 / L3 敏感（默认禁止外发云端）/ L4 机密（不接工具）
LEVEL_LABELS = {'L1': '公开', 'L2': '内部', 'L3': '敏感', 'L4': '机密'}


def _expiry_meta():
    """到期预警参数（records/expiry.py）。

    字典里的窗口天数必须与工作台预警**同源**。以前这里写死 30 天，而预警里
    合同/证照是 60 天，同一个"证照快到期了吗"，工作台说 60 天内都算、
    AI 助手只认 30 天——两个入口两个答案，用户没法解释哪个对。
    """
    try:
        from records.expiry import DEFAULT_LEAD_DAYS, LOOKBACK_DAYS
        return DEFAULT_LEAD_DAYS, LOOKBACK_DAYS
    except Exception:  # noqa: BLE001  应用未就绪时退回默认值
        return {}, 90


_LEAD_DAYS, LOOKBACK_DAYS = _expiry_meta()


def lead_days_of(kind, default=30):
    """某类到期事项的提前提醒天数（保险 30、合同/证照 60 …）。"""
    return int(_LEAD_DAYS.get(kind, default))


INSURANCE_LEAD_DAYS = lead_days_of('insurance', 30)
CONTRACT_LEAD_DAYS = lead_days_of('archive_contract', 60)
LICENSE_LEAD_DAYS = lead_days_of('license_expire', 60)
REVIEW_LEAD_DAYS = lead_days_of('license_review', 30)
BORROW_LEAD_DAYS = lead_days_of('borrow_due', 30)
# 日报粒度是天，不是月：3 天没交才算"快到期"
DAILY_LEAD_DAYS = 3


@dataclass(frozen=True)
class StateSpec:
    """业务状态筛选（"已过期""借出中""逾期"）。

    这些状态在库里不是一个字段：证照"已过期"= 有效期或年审日已过，档案
    "已借出"= 存在未归还的借阅记录。以前只能靠模型自己拼条件，它拼不出来就
    答"没有找到已过期的记录"——把"不会拼条件"说成了"没有数据"。
    把状态定义成可执行的 Q，本地一次过滤就准。

    ``apply`` 给 Q 表达不了的状态用（保险必须"按标的归组取最新年度"，
    "一条标的下有多条保单流水"这种判定不是行级条件）。
    """

    key: str
    label: str
    phrases: tuple = ()          # 用户可能说的词（长的优先匹配）
    q: object = None             # callable(today) -> Q
    apply: object = None         # callable(queryset, today) -> QuerySet
    days: int = 0                # 窗口天数（提前/回溯），0 表示不涉及
    note: str = ''               # 判定口径，进 schema 供模型与人核对

    def filter_q(self, today=None):
        today = today or timezone.localdate()
        return self.q(today) if self.q else Q()

    def apply_to(self, qs, today=None):
        """把状态作用到 queryset 上：优先归组钩子，其次 Q。"""
        today = today or timezone.localdate()
        if self.apply is not None:
            return self.apply(qs, today)
        if self.q is not None:
            return qs.filter(self.q(today))
        return qs


def _open_borrow_q(model_label, fk_name):
    """未归还记录对应的主档：``pk in (select fk from 借阅表 where return_time is null)``。

    不用 ``Q(borrow_records__return_time__isnull=True)``：LEFT JOIN 会把"从来没有
    借阅记录"的行也带出来（全 NULL 行），结果变成"全部档案"。
    """

    def build(_today):
        model = apps.get_model(model_label)
        open_ids = model.objects.filter(return_time__isnull=True).values(f'{fk_name}_id')
        return Q(pk__in=open_ids)

    return build


def _date_between_q(fields, days=30, exclude_expired=True):
    """未来 N 天内到期（可含多个日期口径字段，如有效期 + 下次年审日）。

    排除已过期时用 ``isnull | gte`` 而不是 ``~lt``：SQL 三值逻辑里
    ``NOT (false OR NULL)`` 还是 NULL，用 ``~Q(字段__lt=today)`` 会把
    "年审日为空但有效期未到"的证照整批漏掉。
    """

    def build(today):
        soon = today + timedelta(days=days)
        cond = Q()
        for field in fields:
            cond |= Q(**{f'{field}__gte': today, f'{field}__lte': soon})
        if exclude_expired:
            guard = Q()
            for field in fields:
                guard &= (Q(**{f'{field}__isnull': True})
                          | Q(**{f'{field}__gte': today}))
            cond &= guard
        return cond

    return build


def _date_before_q(fields):
    def build(today):
        cond = Q()
        for field in fields:
            cond |= Q(**{f'{field}__lt': today})
        return cond

    return build


def _blank_q(field):
    """文本字段为空（NULL 或空串）：库里"没填"两种写法都有。"""
    return Q(**{f'{field}__isnull': True}) | Q(**{f'{field}': ''})


def _filled_q(field):
    return ~_blank_q(field)


def _any_text_q(field, words):
    """文本字段命中任一说法（"是/已抵押"这种人工录入的自由文本）。"""
    cond = Q()
    for word in words:
        cond |= Q(**{f'{field}__icontains': word})
    return cond


def _insurance_subject_filter(kind):
    """标的级状态：先按标的归组取最新年度，再挑出符合条件的保单记录。

    保险的"到期"不是单条保单的属性——一个标的下挂着多个年度的流水，
    必须看"最新年度"那一条；否则已续保的老年度会天天报到期（2026-10 修过的
    bug）。这种判定是 QuerySet 级的，纯 Q 表达不了，所以走 ``apply`` 钩子。
    """

    def apply(qs, today):
        from records.insurance_grouping import build_subjects

        try:
            from records.insurance_services import one_off_keys
            one_off = one_off_keys()
        except Exception:  # noqa: BLE001  一次性标的标记缺失时按无标记处理
            one_off = set()

        records = list(qs.select_related('subject', 'policy_year'))
        ids = []
        for subject in build_subjects(records, today=today):
            if subject.key in one_off:
                continue
            latest = subject.latest
            if kind == 'expiring':
                hit = subject.status == 'expiring'
            elif kind == 'expired':
                hit = (subject.status == 'expired'
                       and (subject.expired_days or 0) <= LOOKBACK_DAYS)
            elif kind == 'renewed_this_year':
                hit = latest.end >= today and latest.start.year == today.year
            else:
                hit = False
            if hit:
                ids.extend(r.pk for r in latest.records)
        return qs.filter(pk__in=ids)

    return apply


def _sla_breach_filter(qs, today):
    """超 SLA：未闭环 且 申请时间 + SLA 小时数 已经过了。

    数据库端算不了"日期 + N 小时"（SQLite 的日期函数各版本不一），
    而未闭环客诉是个很小的集合，取出来在 Python 里比更快也更准。
    """
    from datetime import datetime, time

    now = timezone.localtime()
    ids = []
    for obj in qs.filter(closed_at__isnull=True, sla_hours__gt=0).only(
            'pk', 'complaint_date', 'sla_hours'):
        if not obj.complaint_date:
            continue
        deadline = datetime.combine(obj.complaint_date, time.min) + timedelta(
            hours=obj.sla_hours)
        if timezone.make_aware(deadline) < now:
            ids.append(obj.pk)
    return qs.filter(pk__in=ids)


@dataclass(frozen=True)
class ModuleSpec:
    key: str                      # 模块 key（工具参数用；全局唯一）
    label: str
    model: str                    # 'app_label.ModelName'
    level: str
    date_field: str = ''
    keyword_fields: tuple = ()
    list_fields: tuple = ()
    detail_fields: tuple = ()
    group_fields: tuple = ()
    sum_fields: tuple = ()
    order_fields: tuple = ()
    scope: str = 'all'            # 'own' = 仅本人数据；'visible' = 公开+本人
    title_field: str = ''
    detail_route: str = ''
    list_route: str = ''
    sensitive_fields: tuple = ()  # 需强制脱敏的字段（补充字段名规则）
    # RBAC 鉴权 key：一个业务模块（如「试样管理」）下挂多个实体时，
    # 各实体用不同 key 便于区分，但权限仍统一查 samples.read。
    perm_key: str = ''
    soft_delete_field: str = ''   # 软删标记字段（如 is_deleted），查询时自动排除
    parent_field: str = ''        # 明细挂主档时，详情页要带父对象 id（sample/partner）
    states: tuple = ()            # StateSpec 列表：业务状态筛选
    base_filter: object = None    # callable(user) -> Q：模块级的固定过滤（如"已发布"）
    # 到期提前提醒天数（0 = 该模块没有"到期"概念）。
    # 用户说"快到期"而模块又没声明对应状态时，planner 按这个天数自动构造
    # "日期字段落在 [今天, 今天+N]" 的窗口——否则"快到期"这三个字会被整句丢掉，
    # 只剩一个全表合计数。
    lead_days: int = 0
    # 确实没有业务状态的模块，显式写一句原因。模型据此不再硬造 state 参数，
    # 也方便字典校验区分"本来就没有"与"漏了"。
    no_state_reason: str = ''
    # 可排序字段的**口语别名**：((字段名, '阅读', '浏览量', …), …)。
    # 「知识库中阅读最多的一篇」里的"阅读"不是任何字段的值，而是 view_count
    # 这个可排序字段的口语说法。不声明这一步，问题会退化成全文检索 → 0 条，
    # 于是把"我不会排序"答成了"没有这条记录"。
    rank_fields: tuple = ()


# 模块的口语别名。放在字典层而不是 planner：工具层剥"残余关键词"时也要知道
# "审厂""客诉"是模块名而不是筛选条件——带着它去 icontains，只会把条件查成 0 条。
MODULE_ALIASES = {
    'customer_complaint': ('客诉', '投诉', '客户投诉', '客诉记录'),
    'complaint': ('诉讼', '起诉', '案件', '官司', '诉讼案件', '被告'),
    'insurance': ('保险', '保单', '保费', '续保', '保险单', '投保'),
    'customer_ledger': ('台账', '客户档案', '客户信息', '客户资料', '客户清单'),
    'customer_credit': ('账期', '信用额度', '授信', '账期申请', '额度申请'),
    'decoration': ('装修', '装修项目', '工程装修'),
    'daily_record': ('工作记录', '日报', '日志', '每日', '工作日报', '日记'),
    'regulation': ('制度', '规定', '管理办法', '处罚', '规章制度', '条例'),
    'price_evaluation': ('核价', '比价', '询价', '核价单', '核价结果'),
    'metal_price': ('金属', '铜价', '铝价', '钢价', '金属价格', '有色金属'),
    'sample': ('样品', '送样', '打样', '样品送样', '来样'),
    'sample_test': ('试样', '试产', '测试记录', '试样试产', '打样测试'),
    'sample_batch': ('小批量', '批次', '小批量试产', '批量验证'),
    'sample_audit': ('审厂', '验厂', '审厂记录', '工厂审核'),
    'sample_partner': ('供应商', '厂商', '合作伙伴', '供应商档案'),
    'archive': ('档案', '合同档案', '档案资料', '归档', '合同'),
    'archive_borrow': ('档案借阅', '借阅记录', '借阅', '借出记录', '归还记录'),
    'license': ('证照', '资质', '证书', '营业执照', '许可证', '资质证书'),
    'license_review': ('年审', '证照年审', '年审记录', '年检'),
    'license_borrow': ('证照借阅', '证照借出', '证照归还', '证照借用'),
    'knowledge': ('知识库', '知识', '文档', '知识文档', '资料库', 'sop'),
}


MODULES = (
    ModuleSpec(
        key='customer_complaint', label='客户客诉', level='L3',
        model='records.CustomerComplaint',
        date_field='complaint_date',
        keyword_fields=('customer_name', 'responsible_salesperson', 'terminal_customer',
                        'product_model', 'complaint_description', 'remarks'),
        list_fields=('customer_name', 'complaint_date', 'complaint_status',
                     'responsible_salesperson', 'compensation_amount'),
        detail_fields=('customer_name', 'responsible_salesperson', 'complaint_date',
                       'terminal_customer', 'product_model', 'complaint_description',
                       'compensation_amount', 'complaint_status', 'legal_stage',
                       'follow_up_status', 'engineering_analysis', 'missing_materials',
                       'closure_result', 'closed_at', 'is_insured', 'sla_hours',
                       'closure_note', 'engineering_report', 'remarks'),
        group_fields=('complaint_status', 'legal_stage', 'responsible_salesperson',
                      'customer_name', 'closure_result', 'is_insured'),
        sum_fields=('compensation_amount', 'our_shipment', 'customer_shipment'),
        order_fields=('complaint_date', 'compensation_amount', 'complaint_status',
                      'closed_at', 'sla_hours'),
        title_field='customer_name',
        detail_route='records:customer_detail',
        # 「赔了多少钱」是最自然的口语说法：只认"赔偿金额"，这句就选不出字段，
        # 本地求和接不住，落到模型手里就成了编数字（实测答 40000，真值 80000）。
        rank_fields=(('compensation_amount', '赔偿金额', '赔偿', '赔付', '索赔', '赔'),),
        list_route='records:customer_complaint_list',
        states=(
            StateSpec('open', '未完成',
                      ('还没处理', '待处理', '没处理', '未闭环'),
                      lambda today: Q(complaint_status='未完成')),
            StateSpec('processing', '处理中',
                      ('处理中', '跟进中', '正在处理', '在跟进'),
                      lambda today: Q(complaint_status='处理中')),
            # 「已完成/未完成」本身是字段选项词，matched_labels 已经能当筛选条件用；
            # 写进 phrases 会让"已完成的装修"被判成客诉模块，这里只留口语说法。
            StateSpec('closed', '已闭环',
                      ('已闭环', '闭环', '结案了', '处理完了', '搞定了'),
                      lambda today: Q(complaint_status='已完成')),
            StateSpec('with_legal', '涉及法律程序',
                      ('涉及法律', '走诉讼', '打官司', '法律程序', '起诉'),
                      lambda today: Q(legal_stage__in=('准备诉讼', '诉讼中', '已判决'))),
            StateSpec('with_insurance', '走保险理赔',
                      ('报保险', '走理赔', '保险理赔', '理赔'),
                      lambda today: Q(is_insured=True) | Q(legal_stage='保险理赔')),
            StateSpec('sla_breach', '超 SLA',
                      ('超时', '超 SLA', '超SLA', '超过时限', '逾期未处理'),
                      apply=_sla_breach_filter,
                      note='未闭环 且 申请时间+SLA小时数 已经过去'),            StateSpec('accepted', '客户接受',
                      ('客户接受', '接受', '谈成了', '同意方案', 'accepted'),
                      lambda today: Q(closure_result='accepted')),
            StateSpec('partial', '部分接受',
                      ('部分接受', '部分同意', 'partial'),
                      lambda today: Q(closure_result='partial')),
            StateSpec('rejected', '客户不接受',
                      ('不接受', '不认可', '谈崩了', '拒绝', 'rejected'),
                      lambda today: Q(closure_result='rejected')),
            StateSpec('compensated', '有赔偿',
                      ('有赔偿', '赔了钱', '已赔付', '赔付了'),
                      lambda today: Q(compensation_amount__gt=0)),
        ),
    ),
    ModuleSpec(
        key='complaint', label='诉讼', level='L3',
        model='records.Complaint',
        date_field='filed_at',
        keyword_fields=('dispute_type', 'defendant', 'case_number', 'court',
                        'lawyer', 'progress', 'remarks'),
        list_fields=('dispute_type', 'defendant', 'case_number', 'progress',
                     'filed_at', 'amount'),
        detail_fields=('dispute_type', 'defendant', 'case_number', 'amount', 'court',
                       'lawyer', 'progress', 'detailed_progress', 'filed_at',
                       'hearing_at', 'judgment_at', 'execution_at', 'closed_at', 'remarks'),
        group_fields=('progress', 'dispute_type', 'court', 'lawyer'),
        sum_fields=('amount',),
        order_fields=('filed_at', 'amount', 'progress', 'hearing_at', 'judgment_at',
                      'execution_at', 'closed_at'),
        title_field='defendant',
        detail_route='records:complaint_detail',
        rank_fields=(('amount', '涉案金额', '标的额', '诉讼金额'),),
        list_route='records:complaint_list',
        states=(
            StateSpec('filed', '立案阶段',
                      ('立案', '刚立案', '已立案'),
                      lambda today: Q(progress='立案')),
            StateSpec('hearing', '审理中',
                      ('审理中', '在审', '正在打', '开庭审理'),
                      lambda today: Q(progress='审理中')),
            StateSpec('upcoming_hearing', '待开庭',
                      ('待开庭', '要开庭', '下个月开庭', '快开庭', '即将开庭'),
                      lambda today: (Q(hearing_at__gte=today)
                                     & Q(progress__in=('立案', '审理中')))),
            StateSpec('judged', '已判决',
                      ('已判决', '判了', '判决书', '判决下来'),
                      lambda today: Q(progress='判决') | Q(judgment_at__isnull=False)),
            StateSpec('executing', '执行中',
                      ('执行中', '强制执行', '在执行'),
                      lambda today: Q(progress='执行')),
            StateSpec('closed', '已结案',
                      ('已结案', '结了', '完结', '结案了'),
                      lambda today: Q(progress='结案') | Q(closed_at__isnull=False)),
            StateSpec('overdue_stale', '长期未推进',
                      ('拖着的', '很久没动', '长期未推进', '停滞', '压着'),
                      lambda today: (Q(progress__in=('立案', '审理中'))
                                     & Q(filed_at__lt=today - timedelta(days=180))),
                      days=180,
                      note='立案或审理中，且立案日期超过 180 天'),
        ),
    ),
    ModuleSpec(
        key='insurance', label='保险', level='L3',
        model='records.Insurance',
        date_field='end_date',
        keyword_fields=('company_name', 'canonical_name', 'title', 'order_number',
                        'department', 'remarks'),
        list_fields=('company_name', 'title', 'order_number', 'end_date',
                     'premium', 'department'),
        detail_fields=('company_name', 'canonical_name', 'title', 'order_number',
                       'premium', 'issue_date', 'end_date', 'department', 'remarks'),
        group_fields=('company_name', 'canonical_name', 'department'),
        sum_fields=('premium',),
        order_fields=('end_date', 'issue_date', 'premium'),
        title_field='title',
        detail_route='records:insurance_detail',
        rank_fields=(('premium', '保费', '保险金额'),),
        list_route='records:insurance_list',
        sensitive_fields=('order_number',),
        # 保险的空窗期判定与工作台同源：提前 30 天提醒
        lead_days=INSURANCE_LEAD_DAYS,
        states=(
            # 「即将到期」不能按单条保单的 end_date 判：一个标的下有多条年度流水，
            # 已续保的老年度 end_date 早就过了，逐条判会天天报到期（修过的 bug）。
            # 必须按标的归组取最新年度——这是 QuerySet 级操作，用 apply 钩子。
            StateSpec('expiring', '即将到期',
                      ('即将到期', '快到期', '要到期', '临期', '近期到期',
                       '快过期', '马上到期', '快到起期'),
                      apply=_insurance_subject_filter('expiring'),
                      days=INSURANCE_LEAD_DAYS,
                      note='按标的最新保单年度判定，截止日期在未来 30 天内'),
            StateSpec('expired', '已过期',
                      ('已过期', '过期了', '断保', '已失效', '脱保', '应续未续', '未续保'),
                      apply=_insurance_subject_filter('expired'),
                      days=LOOKBACK_DAYS,
                      note='按标的最新保单年度判定，且过期不超过 90 天（工作台称"应续未续"）'),
            StateSpec('renewed_this_year', '本年已续保',
                      ('今年续过', '本年续保', '已续保', '续保了', '刚续保'),
                      apply=_insurance_subject_filter('renewed_this_year'),
                      note='最新保单年度的起期在本年且尚未到期'),
            StateSpec('no_subject', '未登记标的',
                      ('没写标的', '未登记标的', '标的不全', '没有标的'),
                      lambda today: Q(subject__isnull=True)),
        ),
    ),
    ModuleSpec(
        key='customer_ledger', label='客户台账', level='L3',
        model='records.CustomerLedger',
        date_field='contract_sign_date',
        keyword_fields=('customer_name', 'responsible_salesperson', 'legal_representative',
                        'credit_code', 'address', 'remarks'),
        list_fields=('customer_name', 'responsible_salesperson', 'customer_category',
                     'payment_terms', 'credit_limit', 'company_status'),
        detail_fields=('customer_name', 'customer_category', 'responsible_salesperson',
                       'legal_representative', 'address', 'credit_code', 'company_status',
                       'payment_terms', 'credit_limit', 'basic_contract', 'contract_sign_date',
                       'legal_id_card', 'asset_guarantor', 'handling_suggestion',
                       'contract_issues', 'asset_guarantee', 'guaranteed_asset_detail',
                       'asset_mortgaged', 'asset_guarantee_issues', 'contract_count',
                       'archive_no', 'archive_date', 'change_date', 'business_license',
                       'registered_capital_text', 'paid_in_capital_text',
                       'credit_limit_text', 'guarantor_id_card', 'location',
                       'system_info', 'remarks'),
        group_fields=('customer_category', 'company_status', 'responsible_salesperson',
                      'payment_terms'),
        sum_fields=('credit_limit', 'registered_capital', 'paid_in_capital'),
        order_fields=('contract_sign_date', 'credit_limit', 'customer_name',
                      'archive_date', 'change_date'),
        title_field='customer_name',
        detail_route='records:customer_ledger_detail',
        rank_fields=(('credit_limit', '额度', '授信', '信用额度', '账期额度'),
                     ('registered_capital', '注册资金', '注册资本')),
        list_route='records:customer_ledger_list',
        sensitive_fields=('legal_id_card', 'guarantor_id_card'),
        states=(
            StateSpec('archived', '已归档',
                      ('已归档', '归档了', '存过档'),
                      lambda today: (Q(archive_date__isnull=False)
                                     & _filled_q('archive_no'))),
            StateSpec('not_archived', '未归档',
                      ('未归档', '没归档', '还没归档'),
                      lambda today: Q(archive_date__isnull=True)),
            StateSpec('contract_issue', '合同有问题',
                      ('合同有问题', '合同缺件', '合同不全', '合同瑕疵'),
                      lambda today: _filled_q('contract_issues')),
            StateSpec('guarantee_missing', '担保缺失',
                      ('没有担保', '缺担保', '担保缺失', '无担保'),
                      lambda today: _blank_q('asset_guarantee') | _blank_q('asset_guarantor')),
            StateSpec('asset_mortgaged', '担保资产已抵押',
                      ('已抵押', '抵押了', '资产抵押'),
                      lambda today: _any_text_q('asset_mortgaged', ('是', '已抵押', '有抵押'))),
            StateSpec('guarantee_issue', '担保有问题',
                      ('担保有问题', '担保瑕疵', '担保不全'),
                      lambda today: _filled_q('asset_guarantee_issues')),
            StateSpec('credit_changed', '额度或账期变更过',
                      ('调整过额度', '改过账期', '额度变更', '账期变更'),
                      lambda today: Q(change_date__isnull=False)),
            StateSpec('high_credit', '高额度',
                      ('额度高', '大额客户', '高额度', '额度大于500'),
                      lambda today: Q(credit_limit__gte=500),
                      note='额度（万元）≥ 500'),
        ),
    ),
    ModuleSpec(
        key='customer_credit', label='客户账期', level='L3',
        model='records.CustomerCredit',
        date_field='proposal_date',
        keyword_fields=('customer_name', 'salesperson', 'work_content',
                        'specific_content', 'result'),
        list_fields=('customer_name', 'proposal_date', 'salesperson',
                     'work_content', 'result'),
        detail_fields=('customer_name', 'proposal_date', 'site_date', 'customer_type',
                       'salesperson', 'work_content', 'specific_content', 'work_method',
                       'pre_survey', 'result', 'has_assessment_period',
                       'assessment_start_date', 'assessment_end_date',
                       'assessment_content', 'remarks'),
        group_fields=('customer_type', 'work_content', 'work_method', 'salesperson',
                      'result'),
        sum_fields=(),
        order_fields=('proposal_date', 'customer_name', 'site_date',
                      'assessment_end_date'),
        title_field='customer_name',
        detail_route='records:customer_credit_detail',
        list_route='records:customer_credit_list',
        states=(
            # result 是自由文本（没有 choices），人工可能写"通过/批准"，
            # 所以用 __in 容错，只认死一个词会漏掉一半数据。
            StateSpec('agreed', '同意',
                      ('同意', '批了', '通过了', '批准'),
                      lambda today: Q(result__in=('同意', '通过', '批准'))),
            StateSpec('rejected', '不同意',
                      ('不同意', '拒了', '没批', '驳回', '拒绝'),
                      lambda today: Q(result__in=('不同意', '拒绝', '驳回'))),
            StateSpec('pending', '待定',
                      ('待定', '还没结果', '悬着', '待确认'),
                      lambda today: Q(result__in=('待定', '待确认', '未定'))),
            StateSpec('in_assessment', '考核期内',
                      ('考核期内', '还在考核', '考核中'),
                      lambda today: (Q(has_assessment_period=True)
                                     & Q(assessment_start_date__lte=today)
                                     & Q(assessment_end_date__gte=today))),
            StateSpec('assessment_due', '考核期将满',
                      ('考核快到期', '考核要结束', '考核期将满', '快考核完'),
                      lambda today: (Q(has_assessment_period=True)
                                     & Q(assessment_end_date__gte=today)
                                     & Q(assessment_end_date__lte=today
                                         + timedelta(days=30))),
                      days=30),
            StateSpec('site_visit', '已现场审核',
                      ('现场审核', '去过现场', '现场审过'),
                      lambda today: Q(site_date__isnull=False)),
        ),
    ),
    ModuleSpec(
        key='decoration', label='装修', level='L2',
        model='records.Decoration',
        date_field='start_date',
        keyword_fields=('project_name', 'constructor', 'project_address'),
        list_fields=('project_name', 'constructor', 'start_date', 'final_price'),
        detail_fields=('project_name', 'constructor', 'project_address', 'start_date',
                       'original_price', 'final_price', 'reduced_price'),
        group_fields=('constructor',),
        sum_fields=('original_price', 'final_price', 'reduced_price'),
        order_fields=('start_date', 'final_price'),
        title_field='project_name',
        detail_route='records:decoration_detail',
        list_route='records:decoration_list',
        no_state_reason='装修记录没有业务状态字段，只能按日期/金额/施工方筛选',
    ),
    ModuleSpec(
        key='daily_record', label='每日记录', level='L2',
        model='records.DailyRecord',
        date_field='record_date',
        keyword_fields=('department', 'work_content', 'status'),
        list_fields=('record_date', 'department', 'work_content', 'status', 'progress'),
        detail_fields=('record_date', 'department', 'work_content', 'due_date',
                       'status', 'progress'),
        group_fields=('department', 'status'),
        sum_fields=('progress',),
        order_fields=('record_date', 'progress', 'due_date'),
        scope='own',
        title_field='work_content',
        detail_route='records:daily_record_detail',
        list_route='records:daily_record_list',
        # 日报按天算，3 天没交就算"快到期"，套 30 天窗口等于没有预警
        lead_days=DAILY_LEAD_DAYS,
        states=(
            StateSpec('done', '已完成',
                      ('已完成', '完成了', '写完了', '做完了'),
                      lambda today: Q(status='完成')),
            StateSpec('open', '未完成',
                      ('未完成', '没完成', '还没写', '没做完'),
                      lambda today: Q(status='未完成')),
            StateSpec('overdue', '已逾期',
                      ('逾期', '超期', '过期没做', '拖期'),
                      lambda today: Q(status='未完成', due_date__lt=today)),
            StateSpec('due_soon', '即将到期',
                      ('即将到期', '快到期', '这两天要交', '马上要交'),
                      lambda today: (Q(status='未完成')
                                     & Q(due_date__gte=today)
                                     & Q(due_date__lte=today
                                         + timedelta(days=DAILY_LEAD_DAYS))),
                      days=DAILY_LEAD_DAYS),
        ),
    ),
    ModuleSpec(
        key='regulation', label='制度及处罚', level='L1',
        model='records.Regulation',
        date_field='issue_date',
        keyword_fields=('title', 'content', 'doc_no'),
        list_fields=('title', 'doc_no', 'issue_date'),
        detail_fields=('title', 'doc_no', 'issue_date', 'content'),
        group_fields=(),
        sum_fields=(),
        order_fields=('issue_date', 'title'),
        title_field='title',
        detail_route='records:regulation_detail',
        list_route='records:regulation_list',
        no_state_reason='制度是静态文档，没有流转状态',
    ),
    ModuleSpec(
        key='price_evaluation', label='核价', level='L1',
        model='records.PriceEvaluation',
        date_field='',
        keyword_fields=('project_category', 'proposing_department', 'proposer',
                        'evaluation_content'),
        list_fields=('project_category', 'proposer', 'internal_control_supplier',
                     'internal_control_price'),
        detail_fields=('project_category', 'proposing_department', 'proposer',
                       'evaluation_content', 'supplier1_name', 'supplier1_price',
                       'supplier2_name', 'supplier2_price', 'supplier3_name',
                       'supplier3_price', 'internal_control_supplier',
                       'internal_control_price'),
        group_fields=('project_category', 'proposing_department', 'proposer'),
        sum_fields=('supplier1_price', 'supplier2_price', 'supplier3_price',
                    'internal_control_price'),
        order_fields=('internal_control_price', 'project_category'),
        title_field='project_category',
        detail_route='records:price_evaluation_detail',
        list_route='records:price_evaluation_list',
        no_state_reason='核价单没有状态字段，只有报价与内控价，按项目/部门/供应商查',
    ),
    ModuleSpec(
        key='metal_price', label='金属价格', level='L1',
        model='records.MetalPrice',
        date_field='crawl_date',
        keyword_fields=('metal_name', 'unit'),
        list_fields=('metal_name', 'price', 'unit', 'price_change', 'crawl_date'),
        detail_fields=('metal_name', 'price', 'unit', 'price_change', 'crawl_date',
                       'crawl_time'),
        group_fields=('metal_name', 'unit'),
        sum_fields=('price', 'price_change'),
        order_fields=('crawl_date', 'price', 'crawl_time'),
        title_field='metal_name',
        detail_route='records:metal_price_detail',
        rank_fields=(('price', '价格', '报价', '单价'),),
        list_route='',
        no_state_reason='金属价格是按日抓取的行情，没有业务状态',
    ),
    # ---------------------------------------------------------------- 试样管理
    # 一个 RBAC 模块（samples）下挂 5 个实体。以前整块没接入，用户问「试样管理
    # 里有哪些记录」时助手只能拿关键词跨表搜，搜不到就答"没有相关记录"——
    # 把"我没接这个模块"说成了"系统里没这条数据"，是比查不到更严重的错误。
    ModuleSpec(
        key='sample', label='样品送样', level='L2', perm_key='samples',
        model='samples.Sample',
        date_field='sample_date',
        keyword_fields=('sample_no', 'material_name', 'model', 'spec', 'registrar'),
        list_fields=('sample_no', 'material_name', 'model', 'sample_date', 'status',
                     'supplier_type'),
        detail_fields=('sample_no', 'material_name', 'model', 'spec', 'sample_date',
                       'status', 'incoming_qty', 'supplier_type', 'registrar',
                       'registered_at', 'terminate_reason', 'terminated_by',
                       'terminated_at', 'imported_by', 'imported_at'),
        group_fields=('status', 'supplier_type'),
        sum_fields=(),
        order_fields=('sample_date', 'sample_no', 'status', 'registered_at'),
        title_field='material_name',
        detail_route='samples:detail',
        list_route='samples:list',
        soft_delete_field='is_deleted',
        states=(
            # status 是英文码（pending/testing/…），显示是中文。
            # phrases 两套都写：用户说"试样中"，库里的值是 testing。
            StateSpec('pending', '待送样',
                      ('待送样', '还没送', '待测试', 'pending'),
                      lambda today: Q(status='pending')),
            StateSpec('testing', '试样中',
                      ('试样中', '在测', '测试中', '试产中', 'testing'),
                      lambda today: Q(status='testing')),
            StateSpec('small_batch', '小批量中',
                      ('小批量中', '小批量', 'small_batch'),
                      lambda today: Q(status='small_batch')),
            StateSpec('imported', '已正式导入',
                      ('已导入', '正式导入', 'imported'),
                      lambda today: Q(status='imported')),
            StateSpec('terminated', '已终止',
                      ('已终止', '终止了', '停了', '不做了', 'terminated'),
                      lambda today: Q(status='terminated')),
            StateSpec('long_stuck', '长期停滞',
                      ('卡住的', '很久没动', '长期停滞', '停滞'),
                      lambda today: (Q(status__in=('testing', 'small_batch'))
                                     & Q(sample_date__lt=today - timedelta(days=60))),
                      days=60,
                      note='试样中或小批量中，且送样时间超过 60 天'),
        ),
    ),
    ModuleSpec(
        key='sample_test', label='试样试产', level='L2', perm_key='samples',
        model='samples.TestRecord',
        date_field='test_date',
        keyword_fields=('test_no', 'test_result', 'review_conclusion', 'report_code',
                        'operator', 'remark'),
        list_fields=('test_no', 'round_no', 'test_date', 'review_conclusion',
                     'report_code', 'operator'),
        detail_fields=('test_no', 'round_no', 'test_date', 'test_result',
                       'review_conclusion', 'report_code', 'package_type',
                       'incoming_date', 'operator', 'notified', 'operated_at', 'remark'),
        group_fields=('review_conclusion', 'operator', 'notified'),
        sum_fields=(),
        order_fields=('test_date', 'round_no', 'test_no'),
        title_field='test_no',
        detail_route='samples:detail',
        list_route='samples:list',
        parent_field='sample',
        states=(
            StateSpec('passed', '通过',
                      ('通过', '合格', 'pass'),
                      lambda today: Q(review_conclusion='pass')),
            StateSpec('failed', '不通过',
                      ('不通过', '不合格', '没过', '没通过', '未通过', 'fail'),
                      lambda today: Q(review_conclusion='fail')),
            # "待改进"≠"不通过"，合并会让返工率算错
            StateSpec('improve', '待改进',
                      ('待改进', '要改进', '需改进', 'improve'),
                      lambda today: Q(review_conclusion='improve')),
            StateSpec('untested', '未出结论',
                      ('未出结论', '还没测完', '没有结论', '待评审'),
                      lambda today: _blank_q('review_conclusion')),
            StateSpec('retest', '需重试',
                      ('返工', '重试', '第2轮', '二次试产', '多轮'),
                      lambda today: Q(round_no__gte=2)),
            StateSpec('not_notified', '未通知责任人',
                      ('没通知', '未通知', '漏通知'),
                      lambda today: Q(notified=False)),
        ),
    ),
    ModuleSpec(
        key='sample_batch', label='小批量', level='L2', perm_key='samples',
        model='samples.BatchRecord',
        date_field='operated_at',
        keyword_fields=('batch_no', 'batch_result', 'alternative_model', 'operator',
                        'remark'),
        list_fields=('batch_no', 'round_no', 'ordered', 'batch_result',
                     'kingdee_entered', 'unit_price'),
        detail_fields=('batch_no', 'round_no', 'ordered', 'batch_result',
                       'kingdee_entered', 'unit_price', 'alternative_model',
                       'operator', 'operated_at', 'remark'),
        group_fields=('batch_result', 'ordered', 'kingdee_entered'),
        sum_fields=('unit_price',),
        order_fields=('operated_at', 'round_no', 'unit_price'),
        title_field='batch_no',
        detail_route='samples:detail',
        list_route='samples:list',
        parent_field='sample',
        rank_fields=(('unit_price', '单价', '试样单价', '价格'),),
        states=(
            StateSpec('ongoing', '进行中',
                      ('进行中', '小批在跑', 'ongoing'),
                      lambda today: Q(batch_result='ongoing')),
            StateSpec('passed', '通过',
                      ('小批过了', '合格', 'pass'),
                      lambda today: Q(batch_result='pass')),
            StateSpec('failed', '不通过',
                      ('小批没过', '不合格', '没通过', '未通过', 'fail'),
                      lambda today: Q(batch_result='fail')),
            StateSpec('not_ordered', '未下单',
                      ('未下单', '还没下单', '没下单'),
                      lambda today: Q(ordered=False)),
            StateSpec('not_in_kingdee', '未录金蝶',
                      ('未录金蝶', '金蝶没录', '财务没同步', '没进金蝶'),
                      lambda today: Q(kingdee_entered=False)),
        ),
    ),
    ModuleSpec(
        key='sample_audit', label='审厂', level='L2', perm_key='samples',
        model='samples.AuditRecord',
        date_field='audit_date',
        keyword_fields=('audit_no', 'audit_result', 'audit_remark', 'auditor'),
        list_fields=('audit_no', 'round_no', 'audit_date', 'audit_result', 'auditor'),
        detail_fields=('audit_no', 'round_no', 'audit_date', 'audit_result',
                       'audit_remark', 'auditor', 'operated_at'),
        group_fields=('audit_result', 'auditor'),
        sum_fields=(),
        order_fields=('audit_date', 'round_no', 'audit_no'),
        title_field='audit_no',
        detail_route='samples:partner_detail',
        list_route='samples:partner_list',
        parent_field='partner',
        states=(
            StateSpec('passed', '通过',
                      ('审厂过了', '审厂通过', 'pass'),
                      lambda today: Q(audit_result='pass')),
            # "有条件通过"必须单独存在：并进"通过"会掩盖供应商的整改要求
            StateSpec('conditional', '有条件通过',
                      ('有条件通过', '条件通过', 'conditional'),
                      lambda today: Q(audit_result='conditional')),
            StateSpec('failed', '不通过',
                      ('审厂没过', '审厂不通过', 'fail'),
                      lambda today: Q(audit_result='fail')),
        ),
    ),
    ModuleSpec(
        key='sample_partner', label='供应商档案', level='L2', perm_key='samples',
        model='samples.Partner',
        date_field='created_at',
        keyword_fields=('partner_code', 'name', 'contact_person', 'contact_info'),
        list_fields=('partner_code', 'name', 'contact_person', 'contact_info', 'status'),
        detail_fields=('partner_code', 'name', 'contact_person', 'contact_info',
                       'status', 'created_at'),
        group_fields=('status',),
        sum_fields=(),
        order_fields=('created_at', 'name', 'partner_code'),
        title_field='name',
        detail_route='samples:partner_detail',
        list_route='samples:partner_list',
        states=(
            StateSpec('active', '合作中',
                      ('合作中', '在合作', 'active'),
                      lambda today: Q(status='active')),
            StateSpec('inactive', '已停用',
                      ('已停用', '停用了', '不合作了', 'inactive'),
                      lambda today: Q(status='inactive')),
        ),
    ),
    # ---------------------------------------------------------------- 档案管理
    # 截图（2026-10-31）：问「已借出的档案有哪些」答「目前没有查询到已借出的
    # 档案信息」，问「证照管理中 已过期的有哪些」答「没有找到已过期的记录」。
    # 两个模块压根没进 catalog，助手只能拿"档案/证照"当关键词跨表搜，必然 0 条。
    # 这类"状态型"问题（已借出/已过期/即将到期/逾期）在库里不是单个字段，
    # 因此每个模块除了字段白名单，还要声明 states：把口语状态翻成可执行的 Q。
    ModuleSpec(
        key='archive', label='档案', level='L3', perm_key='archive',
        model='archive.Archive',
        date_field='sign_date',
        keyword_fields=('code', 'name', 'counterpart', 'contract_no', 'tags',
                        'description', 'handler', 'archive_label', 'location_code'),
        list_fields=('code', 'name', 'counterpart', 'sign_date', 'expire_date',
                     'renewal_state', 'department', 'copies'),
        detail_fields=('code', 'name', 'cabinet', 'ledger', 'category', 'tags',
                       'description', 'counterpart', 'contract_no', 'amount',
                       'sign_date', 'expire_date', 'department', 'renewal_state',
                       'security_level', 'handler', 'copies', 'attachment_desc',
                       'location_code', 'layer', 'cell'),
        group_fields=('renewal_state', 'department', 'security_level', 'archive_label'),
        sum_fields=(),
        order_fields=('sign_date', 'expire_date', 'created_at', 'code', 'name'),
        title_field='name',
        detail_route='archive:archive_detail',
        list_route='archive:overview',
        lead_days=CONTRACT_LEAD_DAYS,
        states=(
            StateSpec('borrowed', '借出中',
                      ('已借出', '借出中', '借出的', '外借', '未归还', '还没还'),
                      _open_borrow_q('archive.BorrowRecord', 'archive')),
            # 窗口与工作台预警同源（合同 60 天）。这里曾写死 30 天，导致
            # "证照/合同快到期了吗"在工作台和助手给出两个答案。
            StateSpec('expiring', '即将到期',
                      ('即将到期', '快到期', '要到期', '临期', '近期到期',
                       '30天内到期', '一个月内到期'),
                      lambda today: _date_between_q(
                          ('expire_date',), CONTRACT_LEAD_DAYS)(today)
                      & Q(renewal_state='pending'),
                      days=CONTRACT_LEAD_DAYS,
                      note=f'截止日期在未来 {CONTRACT_LEAD_DAYS} 天内且续签状态为待续'),
            StateSpec('expired', '已过期',
                      ('已过期', '过期', '已到期', '到期了', '已失效', '超期'),
                      lambda today: Q(expire_date__lt=today)),
            StateSpec('renewed', '已续签',
                      ('已续签', '续签了', '续过了'),
                      lambda today: Q(renewal_state='renewed')),
            StateSpec('ended', '已结束',
                      ('已结束', '不续签', '不再续签', '终止合同'),
                      lambda today: Q(renewal_state='ended')),
        ),
    ),
    ModuleSpec(
        key='archive_borrow', label='档案借阅', level='L3', perm_key='archive',
        model='archive.BorrowRecord',
        date_field='borrow_time',
        keyword_fields=('borrower', 'remark'),
        list_fields=('borrower', 'borrow_time', 'due_time', 'return_time', 'status'),
        detail_fields=('borrower', 'borrow_time', 'due_time', 'return_time',
                       'status', 'remark'),
        group_fields=('status', 'borrower'),
        sum_fields=(),
        order_fields=('borrow_time', 'due_time', 'return_time'),
        title_field='borrower',
        detail_route='archive:archive_detail',
        list_route='archive:borrow_list',
        parent_field='archive',
        lead_days=BORROW_LEAD_DAYS,
        states=(
            StateSpec('open', '借出中',
                      ('借出中', '未归还', '没归还', '还没还', '在外'),
                      lambda today: Q(return_time__isnull=True)),
            StateSpec('overdue', '已逾期',
                      ('已逾期', '逾期', '超期未还', '逾期未还'),
                      lambda today: Q(return_time__isnull=True, due_time__lt=today)),
            StateSpec('returned', '已归还',
                      ('已归还', '归还了', '已还'),
                      lambda today: Q(return_time__isnull=False)),
        ),
    ),
    ModuleSpec(
        key='license', label='证照', level='L2', perm_key='archive',
        model='archive.License',
        date_field='expire_date',
        keyword_fields=('name', 'code', 'holder', 'issuer', 'remark'),
        list_fields=('name', 'code', 'holder', 'issuer', 'expire_date',
                     'next_review_date'),
        detail_fields=('name', 'code', 'holder', 'issuer', 'issue_date',
                       'expire_date', 'review_cycle', 'next_review_date', 'remark'),
        group_fields=('holder', 'issuer'),
        sum_fields=(),
        order_fields=('expire_date', 'issue_date', 'next_review_date', 'name'),
        title_field='name',
        detail_route='archive:license_detail',
        list_route='archive:license_list',
        # 证照的"状态"是 property（有效期或年审日已过），不是字段，只能靠 states
        lead_days=LICENSE_LEAD_DAYS,
        states=(
            StateSpec('expired', '已过期',
                      ('已过期', '过期', '已失效', '失效', '到期了', '已到期'),
                      _date_before_q(('expire_date', 'next_review_date'))),
            StateSpec('expiring', '即将到期',
                      ('即将到期', '快到期', '要到期', '临期', '近期到期',
                       '30天内到期', '一个月内到期'),
                      _date_between_q(('expire_date', 'next_review_date'),
                                      LICENSE_LEAD_DAYS),
                      days=LICENSE_LEAD_DAYS,
                      note=f'有效期或下次年审日在未来 {LICENSE_LEAD_DAYS} 天内'),
            StateSpec('borrowed', '已借出',
                      ('已借出', '借出中', '外借', '未归还'),
                      _open_borrow_q('archive.LicenseBorrowRecord', 'license')),
            StateSpec('pending_review', '年审临近',
                      ('年审临近', '要年审了', '快年审'),
                      _date_between_q(('next_review_date',), REVIEW_LEAD_DAYS),
                      days=REVIEW_LEAD_DAYS,
                      note=f'下次年审日在未来 {REVIEW_LEAD_DAYS} 天内'),
            # "有哪些需要年审"问的不是"30 天内要年审"，而是**年审日已经到点、
            # 还没办**的那批。没有这个口径时，"需要年审"四个字被整句丢掉，
            # 答成"证照共 20 份"——数字全真，问题没答。
            StateSpec('review_due', '待年审',
                      ('需要年审', '需年审', '要年审', '该年审', '年审到期',
                       '年审过期', '未年审', '没年审'),
                      lambda today: Q(next_review_date__lte=today),
                      note='下次年审日已到（含已过期未办）'),
        ),
    ),
    ModuleSpec(
        key='license_review', label='证照年审', level='L2', perm_key='archive',
        model='archive.AnnualReview',
        date_field='review_date',
        keyword_fields=('reviewer', 'remark'),
        list_fields=('review_date', 'result', 'reviewer', 'new_expire_date'),
        detail_fields=('review_date', 'result', 'reviewer', 'new_expire_date', 'remark'),
        group_fields=('result', 'reviewer'),
        sum_fields=(),
        order_fields=('review_date',),
        title_field='reviewer',
        detail_route='archive:license_detail',
        list_route='archive:license_list',
        parent_field='license',
        lead_days=REVIEW_LEAD_DAYS,
        states=(
            StateSpec('pending', '待年审',
                      ('待年审', '还没审', '未年审', '待处理'),
                      lambda today: Q(result='pending')),
            StateSpec('failed', '年审未通过',
                      ('年审没通过', '年审未通过', '年审挂了', '未通过'),
                      lambda today: Q(result='failed')),
            StateSpec('passed', '年审通过',
                      ('年审通过', '年审过了', '已通过'),
                      lambda today: Q(result='passed')),
            # 数据完整性：过了年审却没填新有效期，证照页会继续报"即将到期"
            StateSpec('no_new_expire', '年审后未更新有效期',
                      ('没更新有效期', '未更新有效期', '年审后没改'),
                      lambda today: (Q(result='passed')
                                     & Q(new_expire_date__isnull=True))),
        ),
    ),
    ModuleSpec(
        key='license_borrow', label='证照借阅', level='L2', perm_key='archive',
        model='archive.LicenseBorrowRecord',
        date_field='borrow_time',
        keyword_fields=('borrower', 'remark'),
        list_fields=('borrower', 'borrow_time', 'due_time', 'return_time', 'status'),
        detail_fields=('borrower', 'borrow_time', 'due_time', 'return_time',
                       'status', 'remark'),
        group_fields=('status', 'borrower'),
        sum_fields=(),
        order_fields=('borrow_time', 'due_time', 'return_time'),
        title_field='borrower',
        detail_route='archive:license_detail',
        list_route='archive:license_borrow_list',
        parent_field='license',
        lead_days=BORROW_LEAD_DAYS,
        states=(
            StateSpec('open', '借出中',
                      ('借出中', '未归还', '没归还', '还没还', '在外'),
                      lambda today: Q(return_time__isnull=True)),
            StateSpec('overdue', '已逾期',
                      ('已逾期', '逾期', '超期未还', '逾期未还'),
                      lambda today: Q(return_time__isnull=True, due_time__lt=today)),
            StateSpec('returned', '已归还',
                      ('已归还', '归还了', '已还'),
                      lambda today: Q(return_time__isnull=False)),
        ),
    ),
    # ---------------------------------------------------------------- 知识库
    # search_knowledge 工具早就写好了，但 module_key='knowledge' 不在 catalog 里，
    # available_specs 因此永远不把它放出去——工具存在却调不到。补上模块即可。
    ModuleSpec(
        key='knowledge', label='知识库', level='L2', perm_key='knowledge',
        model='knowledge_base.KnowledgeDocument',
        date_field='updated_at',
        keyword_fields=('title', 'summary', 'tags', 'content'),
        list_fields=('title', 'category', 'status', 'updated_at', 'view_count'),
        detail_fields=('title', 'category', 'summary', 'tags', 'status',
                       'visibility', 'updated_at', 'view_count', 'source',
                       'editor_mode'),
        group_fields=('status', 'visibility'),
        sum_fields=('view_count',),
        order_fields=('updated_at', 'view_count', 'title'),
        scope='visible',          # 仅"全员可见 + 本人私有"，别人的私有稿不可见
        title_field='title',
        detail_route='knowledge_base:document_detail',
        rank_fields=(('view_count', '阅读', '浏览量', '浏览', '访问量', '访问',
                      '看过', '热度', '热门', '阅读次数'),),
        list_route='knowledge_base:document_list',
        # 与列表页口径一致（views.document_list 同样只取已发布）：别人的草稿
        # 不该被 AI 搜出来，本人的草稿可以。
        base_filter=lambda user: Q(status='published') | Q(owner=user),
        states=(
            StateSpec('published', '已发布',
                      ('已发布', '发布了的', '正式发布'),
                      lambda today: Q(status='published')),
            StateSpec('draft', '草稿',
                      ('草稿', '还没发布', '未发布'),
                      lambda today: Q(status='draft')),
            StateSpec('private', '私有',
                      ('私有', '仅自己可见', '不公开'),
                      lambda today: Q(visibility='private')),
        ),
    ),
)

MODULE_MAP = {m.key: m for m in MODULES}
MODULE_KEYS = tuple(m.key for m in MODULES)


def perm_of(spec):
    """模块对应的 RBAC 权限 key（一个业务模块下可挂多个实体）。"""
    return spec.perm_key or spec.key

# 数值字段（反射兜底用；catalog 里显式声明的优先）
_NUMERIC_INTERNAL = {
    'DecimalField', 'IntegerField', 'BigIntegerField', 'FloatField',
    'PositiveIntegerField', 'SmallIntegerField', 'PositiveSmallIntegerField',
}


def get_module(key):
    return MODULE_MAP.get(key)


def model_of(spec):
    return apps.get_model(spec.model)


def all_fields(spec):
    """模型上可安全暴露的字段名（排除主键与文件字段）。"""
    names = []
    for f in model_of(spec)._meta.concrete_fields:
        if f.primary_key:
            continue
        if f.get_internal_type() in {'FileField', 'ImageField'}:
            continue
        names.append(f.name)
    return tuple(names)


def field_label(spec, name):
    try:
        return str(model_of(spec)._meta.get_field(name).verbose_name)
    except Exception:
        return name


def field_type(spec, name):
    try:
        return model_of(spec)._meta.get_field(name).get_internal_type()
    except Exception:
        return ''


def is_numeric(spec, name):
    return name in spec.sum_fields or field_type(spec, name) in _NUMERIC_INTERNAL


_TEXT_TYPES = {'CharField', 'TextField', 'SlugField', 'EmailField', 'URLField'}
_DATE_TYPES = {'DateField', 'DateTimeField'}


def choice_fields(spec):
    """模块里带选项的字段：``[(字段名, [(值, 标签), …]), …]``。

    诉讼的「进度」存的就是中文（立案/审理中/判决/执行/结案），样例的状态存的是
    代码（pass/fail）。两种都要能认出来：前者用来判断"用户是不是在按状态分组"，
    后者用来把口语翻译成库里的值。
    """
    key = spec if not isinstance(spec, ModuleSpec) else spec.key
    spec = MODULE_MAP.get(key)
    if spec is None:
        return []
    try:
        model = model_of(spec)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for field in model._meta.concrete_fields:
        if field.get_internal_type() not in _TEXT_TYPES:
            continue
        choices = [(value, str(label)) for value, label in
                   (getattr(field, 'choices', None) or ()) if label]
        if choices:
            out.append((field.name, choices))
    return out


def label_for_value(spec, field_name, value):
    """把库里存的值翻回中文标签（"fail" → "不通过"）；没有选项时原样返回。"""
    for name, choices in choice_fields(spec):
        if name == field_name:
            for candidate, label in choices:
                if candidate == value:
                    return label
    return '' if value in (None, '') else str(value)


def rankable_fields(spec):
    """可排序字段清单：``[(字段名, 中文标签, 口语别名), …]``。

    只放"有大小概念"的字段——数值能给"最多/最大/最高"，日期能给"最新/最早"。
    文本字段（标题、名称）排序对用户没有意义，排除掉可以避免"名称最多的是哪条"
    这种答非所问。
    """
    out = []
    for name in list(spec.sum_fields) + list(spec.order_fields) + [spec.date_field]:
        if not name or any(name == seen for seen, _lb, _al in out):
            continue
        if not (is_numeric(spec, name) or field_type(spec, name) in _DATE_TYPES):
            continue
        aliases = ()
        for entry in spec.rank_fields:
            if entry and entry[0] == name:
                aliases = tuple(a for a in entry[1:] if a)
                break
        out.append((name, field_label(spec, name), aliases))
    return tuple(out)


def search_fields(spec):
    """关键词匹配可用的字段：显式声明的 + 可分组维度（都是纯文本）。

    「账期为月结60天的客户有多少」里，"月结60天"是 ``payment_terms`` 的值，
    但它是自由文本、没写进 ``keyword_fields``；只按原清单匹配会 0 条，
    模型只能答"没有记录"。分组维度本来就已经暴露给模型，纳入搜索不扩大可见面。
    """
    names = list(spec.keyword_fields)
    for name in list(spec.group_fields) + ([spec.title_field] if spec.title_field else []):
        if name and name not in names:
            names.append(name)
    try:
        model = model_of(spec)
    except Exception:  # noqa: BLE001
        return tuple(names)
    keep = []
    for name in names:
        try:
            field = model._meta.get_field(name)
        except Exception:  # noqa: BLE001
            continue
        if field.get_internal_type() in _TEXT_TYPES:
            keep.append(name)
    return tuple(keep)


def choice_options(module_key, limit=4):
    """模块里带选项的字段及其选项名，用于告诉模型"能用哪些词筛选"。"""
    out = {}
    for name, choices in choice_fields(module_key):
        out[name] = [label for _value, label in choices][:6]
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------------------------ 业务状态

def get_state(spec, key):
    """按 key 取状态定义（工具参数 state → StateSpec），没有则 None。

    模型经常把口头说法直接当参数传（"已借出"而 key 是 ``borrowed``），
    所以标签与短语都认，只要落在同一个状态上。
    """
    if spec is None or not key:
        return None
    key = str(key).strip()
    for state in spec.states:
        if state.key == key or state.label == key:
            return state
    for state in spec.states:
        if key in state.phrases:
            return state
    return None


# 短语前紧贴这些字时，命中是反着说的（"没通过"里也含"通过"）
_NEGATION_CHARS = frozenset('没无不非未别莫勿')
# 多字否定词：只往前看一个字会漏掉口语里最常见的"没有""并非""从未"——
# 「审厂没有通过的供应商」里"通过"前面是"有"，照着单字规则会判成正说。
_NEGATION_WORDS = ('没有', '并非', '从未', '不曾', '未曾', '尚未')


def phrase_negated(text, index, phrase):
    """``phrase`` 在 ``text[index:]`` 处是否被前面的否定词反说。

    全库只此一份判定：状态词（catalog）与选项标签（tools.common）都调它，
    以前两处各维护一份否定字表、且工具层只看一个字，"没有通过"就被判成了
    "通过"，问"没通过的供应商"答出三条全部通过。

    短语本身以否定字开头时不算反说——"不通过""未通过"就是用户要找的那一类。
    """
    if not phrase or index <= 0:
        return False
    if phrase[0] in _NEGATION_CHARS:
        return False
    head = text[:index]
    return head.endswith(_NEGATION_WORDS) or head[-1] in _NEGATION_CHARS


def phrase_hit(text, phrase):
    """``phrase`` 是否在 ``text`` 里出现过且**至少有一处不是反说**。"""
    if not text or not phrase:
        return False
    start = 0
    while True:
        index = text.find(phrase, start)
        if index < 0:
            return False
        if not phrase_negated(text, index, phrase):
            return True
        start = index + 1


def _best_state_phrase(spec, text):
    """最长且未被否定的状态短语 -> ``(state, phrase)``；没命中返回 ``(None, '')``。"""
    if spec is None or not text:
        return None, ''
    best_state, best_word, best_len = None, '', 0
    for state in spec.states:
        for phrase in state.phrases:
            if not phrase:
                continue
            start = 0
            while True:
                idx = text.find(phrase, start)
                if idx < 0:
                    break
                if not phrase_negated(text, idx, phrase) and len(phrase) > best_len:
                    best_state, best_word, best_len = state, phrase, len(phrase)
                start = idx + 1
    return best_state, best_word


def state_match(spec, text):
    """问题里点了某个业务状态就返回它（长词优先，避免"即将到期"被"已到期"抢走）。

    否定词敏感：直接子串匹配会把反话说成正话——
    「试样试产中没通过的有哪些」里也含"通过"，以前会答成"通过的有几条"，
    正好反了。短语前面紧贴否定词时不算命中，除非短语本身就以否定词开头
    （"不通过""未通过"这类整体是否定的短语不受影响）。
    """
    return _best_state_phrase(spec, text)[0]


def state_match_with_word(spec, text):
    """同 :func:`state_match`，但同时返回命中的短语（供纠偏判断词的具体度）。"""
    return _best_state_phrase(spec, text)


def state_options(spec):
    """给模型看的可用状态：``['已过期', '即将到期']``。"""
    return [state.label for state in spec.states]


def schema(spec):
    """给模型看的模块说明（中文标签 + 可用操作）。"""

    out = {
        'module': spec.key,
        'label': spec.label,
        'list_fields': {f: field_label(spec, f) for f in spec.list_fields},
        'keywords': [field_label(spec, f) for f in spec.keyword_fields],
        'date_field': (field_label(spec, spec.date_field) if spec.date_field else ''),
        'group_by': {f: field_label(spec, f) for f in spec.group_fields},
        'sum': {f: field_label(spec, f) for f in spec.sum_fields},
        'states': state_options(spec),
    }
    if spec.lead_days:
        out['lead_days'] = spec.lead_days
        out['expiring_hint'] = (
            f'"即将到期/快到期"= {spec.date_field} 在未来 {spec.lead_days} 天内')
    if not spec.states:
        out['no_states'] = spec.no_state_reason or '该模块没有业务状态'
    notes = {s.label: s.note for s in spec.states if s.note}
    if notes:
        out['state_notes'] = notes
    return out


def safe_reverse(route, args=None):
    """解析路由名；兼容"有命名空间"与"全局路由名"两种注册方式。

    records/urls.py 没有 app_name（路由名是全局的），而 knowledge_base 有；
    这里两种都试一次，避免新增模块时因为命名空间差异导致引用链接为空。
    """
    if not route:
        return ''
    candidates = [route]
    if ':' in route:
        candidates.append(route.split(':', 1)[1])
    for name in candidates:
        try:
            return reverse(name, args=args or [])
        except NoReverseMatch:
            continue
    return ''


def _detail_url(spec, obj):
    if not spec.detail_route:
        return ''
    if spec.key == 'metal_price':
        return safe_reverse(spec.detail_route, [getattr(obj, 'metal_name')])
    if spec.parent_field:
        # 明细（试样/小批量/审厂）没有独立详情页，挂在主档页面上：
        # 链接指向父对象，用户能一眼看到上下文。
        parent_id = getattr(obj, f'{spec.parent_field}_id', None)
        if parent_id:
            return safe_reverse(spec.detail_route, [parent_id])
    return safe_reverse(spec.detail_route, [obj.pk])


def build_ref(spec, obj):
    """构造服务端引用（前端渲染成卡片，模型无权生成 id/链接）。

    只包含定位信息与标题，不含业务字段——引用是"指向"，不是数据载体。
    """
    title = ''
    if spec.title_field:
        title = str(getattr(obj, spec.title_field, '') or '')
    return {
        'module': spec.key,
        'module_label': spec.label,
        'id': obj.pk,
        'label': title[:60] or f'{spec.label}#{obj.pk}',
        'url': _detail_url(spec, obj),
    }


# --------------------------------------------------------------- 字典完整性
# 技术字段与文件/外键字段：**故意**不暴露给模型。列在这里是为了让字典校验
# 能区分"故意不给"与"漏了"——否则每次审计都会被 created_at 这类噪音淹没。
TECHNICAL_FIELDS = frozenset({
    'id', 'created_at', 'updated_at', 'is_deleted', 'deleted_by', 'deleted_at',
    'creator', 'owner', 'user', 'attachment', 'scan_file', 'file', 'tree_version',
    'kb_enabled', 'kb_visibility', 'kb_doc', 'kb_synced_at', 'source_sheet',
})

_NOT_EXPOSABLE_TYPES = {'FileField', 'ImageField', 'ForeignKey', 'OneToOneField',
                        'ManyToManyField'}


def declared_fields(spec):
    """字典里显式声明过的字段名。"""
    names = set()
    for attr in ('keyword_fields', 'list_fields', 'detail_fields',
                 'group_fields', 'sum_fields', 'order_fields'):
        names |= set(getattr(spec, attr))
    for attr in ('date_field', 'title_field'):
        name = getattr(spec, attr)
        if name:
            names.add(name)
    return names


def field_audit(spec):
    """字段字典与真实模型的一致性：``(声明了但不存在, 存在但没声明)``。

    第二种只报"业务字段"：外键、文件、技术字段本来就不该进字典。
    """
    concrete = {f.name: f for f in model_of(spec)._meta.concrete_fields}
    declared = declared_fields(spec)
    missing = sorted(name for name in declared if name not in concrete)
    uncovered = sorted(
        name for name, field in concrete.items()
        if name not in declared
        and not field.primary_key
        and name not in TECHNICAL_FIELDS
        and field.get_internal_type() not in _NOT_EXPOSABLE_TYPES
    )
    return missing, uncovered
