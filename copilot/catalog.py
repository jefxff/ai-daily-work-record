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


@dataclass(frozen=True)
class StateSpec:
    """业务状态筛选（"已过期""借出中""逾期"）。

    这些状态在库里不是一个字段：证照"已过期"= 有效期或年审日已过，档案
    "已借出"= 存在未归还的借阅记录。以前只能靠模型自己拼条件，它拼不出来就
    答"没有找到已过期的记录"——把"不会拼条件"说成了"没有数据"。
    把状态定义成可执行的 Q，本地一次过滤就准。
    """

    key: str
    label: str
    phrases: tuple = ()          # 用户可能说的词（长的优先匹配）
    q: object = None             # callable(today) -> Q

    def filter_q(self, today=None):
        today = today or timezone.localdate()
        return self.q(today) if self.q else Q()


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
                       'closure_result', 'closed_at', 'remarks'),
        group_fields=('complaint_status', 'legal_stage', 'responsible_salesperson',
                      'customer_name'),
        sum_fields=('compensation_amount', 'our_shipment', 'customer_shipment'),
        order_fields=('complaint_date', 'compensation_amount', 'complaint_status'),
        title_field='customer_name',
        detail_route='records:customer_detail',
        list_route='records:customer_complaint_list',
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
        order_fields=('filed_at', 'amount', 'progress'),
        title_field='defendant',
        detail_route='records:complaint_detail',
        list_route='records:complaint_list',
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
        list_route='records:insurance_list',
        sensitive_fields=('order_number',),
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
                       'legal_id_card', 'asset_guarantor', 'handling_suggestion', 'remarks'),
        group_fields=('customer_category', 'company_status', 'responsible_salesperson',
                      'payment_terms'),
        sum_fields=('credit_limit', 'registered_capital', 'paid_in_capital'),
        order_fields=('contract_sign_date', 'credit_limit', 'customer_name'),
        title_field='customer_name',
        detail_route='records:customer_ledger_detail',
        list_route='records:customer_ledger_list',
        sensitive_fields=('legal_id_card', 'guarantor_id_card'),
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
                       'pre_survey', 'result', 'remarks'),
        group_fields=('customer_type', 'work_content', 'work_method', 'salesperson'),
        sum_fields=(),
        order_fields=('proposal_date', 'customer_name'),
        title_field='customer_name',
        detail_route='records:customer_credit_detail',
        list_route='records:customer_credit_list',
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
        order_fields=('record_date', 'progress'),
        scope='own',
        title_field='work_content',
        detail_route='records:daily_record_detail',
        list_route='records:daily_record_list',
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
    ),
    ModuleSpec(
        key='metal_price', label='金属价格', level='L1',
        model='records.MetalPrice',
        date_field='crawl_date',
        keyword_fields=('metal_name', 'unit'),
        list_fields=('metal_name', 'price', 'unit', 'price_change', 'crawl_date'),
        detail_fields=('metal_name', 'price', 'unit', 'price_change', 'crawl_date'),
        group_fields=('metal_name', 'unit'),
        sum_fields=('price', 'price_change'),
        order_fields=('crawl_date', 'price'),
        title_field='metal_name',
        detail_route='records:metal_price_detail',
        list_route='',
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
                       'registered_at', 'terminate_reason'),
        group_fields=('status', 'supplier_type'),
        sum_fields=(),
        order_fields=('sample_date', 'sample_no', 'status'),
        title_field='material_name',
        detail_route='samples:detail',
        list_route='samples:list',
        soft_delete_field='is_deleted',
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
                       'incoming_date', 'operator', 'remark'),
        group_fields=('review_conclusion', 'operator'),
        sum_fields=(),
        order_fields=('test_date', 'round_no', 'test_no'),
        title_field='test_no',
        detail_route='samples:detail',
        list_route='samples:list',
        parent_field='sample',
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
                     'renewal_state', 'department'),
        detail_fields=('code', 'name', 'cabinet', 'ledger', 'category', 'tags',
                       'description', 'counterpart', 'contract_no', 'amount',
                       'sign_date', 'expire_date', 'department', 'renewal_state',
                       'security_level', 'handler', 'copies', 'attachment_desc',
                       'location_code'),
        group_fields=('renewal_state', 'department', 'security_level'),
        sum_fields=(),
        order_fields=('sign_date', 'expire_date', 'created_at', 'code', 'name'),
        title_field='name',
        detail_route='archive:archive_detail',
        list_route='archive:overview',
        states=(
            StateSpec('borrowed', '借出中',
                      ('已借出', '借出中', '借出的', '外借', '未归还', '还没还'),
                      _open_borrow_q('archive.BorrowRecord', 'archive')),
            StateSpec('expiring', '即将到期',
                      ('即将到期', '快到期', '要到期', '临期', '近期到期',
                       '30天内到期', '一个月内到期'),
                      lambda today: _date_between_q(('expire_date',))(today)
                      & Q(renewal_state='pending')),
            StateSpec('expired', '已过期',
                      ('已过期', '过期', '已到期', '到期了', '已失效', '超期'),
                      lambda today: Q(expire_date__lt=today)),
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
        states=(
            StateSpec('expired', '已过期',
                      ('已过期', '过期', '已失效', '失效', '到期了', '已到期'),
                      _date_before_q(('expire_date', 'next_review_date'))),
            StateSpec('expiring', '即将到期',
                      ('即将到期', '快到期', '要到期', '临期', '近期到期',
                       '30天内到期', '一个月内到期'),
                      _date_between_q(('expire_date', 'next_review_date'))),
            StateSpec('borrowed', '已借出',
                      ('已借出', '借出中', '外借', '未归还'),
                      _open_borrow_q('archive.LicenseBorrowRecord', 'license')),
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
                       'visibility', 'updated_at', 'view_count'),
        group_fields=('status', 'visibility'),
        sum_fields=('view_count',),
        order_fields=('updated_at', 'view_count', 'title'),
        scope='visible',          # 仅"全员可见 + 本人私有"，别人的私有稿不可见
        title_field='title',
        detail_route='knowledge_base:document_detail',
        list_route='knowledge_base:document_list',
        # 与列表页口径一致（views.document_list 同样只取已发布）：别人的草稿
        # 不该被 AI 搜出来，本人的草稿可以。
        base_filter=lambda user: Q(status='published') | Q(owner=user),
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


def state_match(spec, text):
    """问题里点了某个业务状态就返回它（长词优先，避免"即将到期"被"已到期"抢走）。"""
    if spec is None or not text:
        return None
    best, best_len = None, 0
    for state in spec.states:
        for phrase in state.phrases:
            if phrase and phrase in text and len(phrase) > best_len:
                best, best_len = state, len(phrase)
    return best


def state_options(spec):
    """给模型看的可用状态：``['已过期', '即将到期']``。"""
    return [state.label for state in spec.states]


def schema(spec):
    """给模型看的模块说明（中文标签 + 可用操作）。"""

    return {
        'module': spec.key,
        'label': spec.label,
        'list_fields': {f: field_label(spec, f) for f in spec.list_fields},
        'keywords': [field_label(spec, f) for f in spec.keyword_fields],
        'date_field': (field_label(spec, spec.date_field) if spec.date_field else ''),
        'group_by': {f: field_label(spec, f) for f in spec.group_fields},
        'sum': {f: field_label(spec, f) for f in spec.sum_fields},
        'states': state_options(spec),
    }


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
