from rest_framework import viewsets, filters, mixins
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from django.utils import timezone
from datetime import datetime, timedelta
import logging
import os

from accounts.services import has_perm
from records.audit import log_operation, snapshot
from samples.services import (
    create_partner, create_sample, create_test_record, create_batch_record,
    BusinessError,
)

logger = logging.getLogger(__name__)


def _can(user, module, action):
    """AI 函数视图的 RBAC 判定（超管在 has_perm 内全部放行）。"""
    return has_perm(user, module, action)


# AI 接口的「模型别名」→ RBAC 权限模块 key 的映射。
# 别名是接口与返回分组使用的标识（如 sample / partner / license），而权限模块 key
# 必须落在 accounts.registry 的合法集合内。二者混用会让 has_perm() 的
# "module not in MODULE_MAP" 分支恒返回 False，导致非超管用户静默搜不到/建不了
# 这些模块的数据（评审 R6；超管因 is_superuser 短路而看不出来）。
_AI_PERM_MODULE = {
    'partner': 'samples',
    'sample': 'samples',
    'test_record': 'samples',
    'batch_record': 'samples',
    'audit_record': 'samples',
    'license': 'archive',
    'cabinet': 'archive',
    'archive_category': 'archive',
    'ledger': 'archive',
    'knowledge_document': 'knowledge',
    'knowledge_category': 'knowledge',
}


def _ai_perm_module(name):
    """模型别名 → 权限模块 key；未登记的别名本身即为合法模块 key。"""
    return _AI_PERM_MODULE.get(name, name)


class CustomPagination(PageNumberPagination):
    """自定义分页类"""
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100
    page_query_param = 'page'


class EnhancedModelViewSet(viewsets.ModelViewSet):
    """增强的ModelViewSet，包含分页、过滤和搜索功能"""
    pagination_class = CustomPagination
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    # RBAC 模块 key（accounts.registry）；子类必须按业务模块覆盖
    perm_module = None

    # ---- 操作留痕配置（AI API 的更新/删除属静默写通道，必须留痕）----
    # 所有模块统一写 records.OperationLog（操作留痕已全站归一，无独立分表）
    # audit_label:        留痕模块名（与页面端留痕一致，如 '每日记录'），为空回退 perm_module
    # audit_object_name:  留痕对象类型名（如 '台账'），为空则用 audit_label
    # audit_display_field:留痕摘要中引用的展示字段（与页面端 display_field 一致）
    # audit_style:        标识符风格，'bracket' -> '[模块]下的记录#id'（records 模块）
    #                     'plain' -> '对象#id'（archive/knowledge/samples 页面端既有风格）
    audit_label = None
    audit_object_name = None
    audit_display_field = None
    audit_style = 'bracket'

    def _audit_label(self):
        return self.audit_label or self.perm_module or '未知模块'

    def _audit_display(self, obj):
        if self.audit_display_field:
            value = getattr(obj, self.audit_display_field, None)
            if value:
                return str(value)
        return f'ID {obj.pk}'

    def _write_audit(self, op_type, obj_id, summary, before=None, after=None):
        """写入操作留痕；留痕失败仅记日志，不影响 API 主流程。

        before/after 为 records.audit.snapshot 归一后的字典：
        新增仅传 after、删除仅传 before、编辑两者都传。
        """
        try:
            label = self._audit_label()
            obj_name = self.audit_object_name or label
            if self.audit_style == 'plain':
                identifier = f'{obj_name}#{obj_id}'
            else:
                identifier = f'[{label}]下的记录#{obj_id}'
            log_operation(self.request, label, op_type, identifier, summary,
                          before=before, after=after)
        except Exception:
            logger.exception('AI API 操作留痕写入失败（%s %s#%s）',
                             op_type, self.audit_label or self.perm_module, obj_id)

    def perform_create(self, serializer):
        """创建同样要留痕。

        改造前只覆写了 update / destroy —— 一条来源不明的记录比一条被修改的
        记录更难追责，创建恰恰是最需要留痕的动作（审计 #8）。
        """
        obj = serializer.save()
        obj_name = self.audit_object_name or self._audit_label()
        display = self._audit_display(obj)
        self._write_audit('新增', obj.pk, f'新增{obj_name}: {display}',
                          after=snapshot(obj))

    def perform_update(self, serializer):
        # 快照须在 save 前拍：save 会就地改写实例
        before_snap = snapshot(serializer.instance)
        obj = serializer.save()
        obj_name = self.audit_object_name or self._audit_label()
        display = self._audit_display(obj)
        self._write_audit('更新', obj.pk, f'编辑{obj_name}: {display}',
                          before=before_snap, after=snapshot(obj))

    def perform_destroy(self, instance):
        # delete() 会将实例 pk 置 None，须先取出再删除（与页面端删除留痕一致）
        obj_id = instance.pk
        before_snap = snapshot(instance)
        display = self._audit_display(instance)
        obj_name = self.audit_object_name or self._audit_label()
        instance.delete()
        self._write_audit('删除', obj_id, f'删除{obj_name}: {display}', before=before_snap)

    def get_queryset(self):
        """重写查询集以支持过滤"""
        queryset = super().get_queryset()

        # 支持日期范围过滤
        start_date = self.request.query_params.get('start_date', None)
        end_date = self.request.query_params.get('end_date', None)

        if start_date and end_date:
            try:
                start = datetime.strptime(start_date, '%Y-%m-%d')
                end = datetime.strptime(end_date, '%Y-%m-%d')

                # 根据模型字段进行过滤
                if hasattr(queryset.model, 'record_date'):
                    queryset = queryset.filter(record_date__range=[start, end])
                elif hasattr(queryset.model, 'issue_date'):
                    queryset = queryset.filter(issue_date__range=[start, end])
                elif hasattr(queryset.model, 'crawl_date'):
                    queryset = queryset.filter(crawl_date__range=[start, end])
                elif hasattr(queryset.model, 'proposal_date'):
                    queryset = queryset.filter(proposal_date__range=[start, end])
                elif hasattr(queryset.model, 'complaint_date'):
                    queryset = queryset.filter(complaint_date__range=[start, end])
                elif hasattr(queryset.model, 'created_at'):
                    # samples/archive/knowledge 等以创建时间兜底
                    queryset = queryset.filter(created_at__range=[start, end])

            except ValueError:
                # 日期格式错误，忽略过滤
                pass
        
        # 支持status过滤 (DailyRecord: '完成'/'未完成', CustomerComplaint: '未完成'/'处理中'/'已完成')
        status_filter = self.request.query_params.get('status', None)
        if status_filter:
            if hasattr(queryset.model, 'status'):
                # DailyRecord: status字段
                queryset = queryset.filter(status=status_filter)
            elif hasattr(queryset.model, 'complaint_status'):
                # CustomerComplaint: complaint_status字段
                queryset = queryset.filter(complaint_status=status_filter)
        
        return queryset


class ReadOnlyEnhancedViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    """只读增强基类：分页/搜索与 EnhancedModelViewSet 一致，仅暴露 list/retrieve。

    用于借阅记录、年审记录等流程类数据（写入走页面业务流程）。
    """

    pagination_class = CustomPagination
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    perm_module = None

    def get_queryset(self):
        # 只读视图无需日期/状态过滤，保持与列表页一致的排序
        return super().get_queryset()


from records.models import (
    Decoration, CustomerCredit, CustomerComplaint, Regulation,
    PriceEvaluation, DailyRecord, MetalPrice, Insurance, Complaint
)
from samples.models import Partner, Sample, TestRecord, BatchRecord, AuditRecord
from archive.models import (
    Cabinet, Category as ArchiveCategory, Ledger, Archive,
    BorrowRecord, License, AnnualReview, LicenseBorrowRecord,
)
from knowledge_base.models import KnowledgeCategory, KnowledgeDocument
from .serializers import (
    DecorationSerializer, CustomerCreditSerializer, CustomerComplaintSerializer,
    RegulationSerializer, PriceEvaluationSerializer, DailyRecordSerializer,
    MetalPriceSerializer, InsuranceSerializer, ComplaintSerializer,
    PartnerSerializer, SampleSerializer, TestRecordSerializer,
    BatchRecordSerializer, AuditRecordSerializer,
    CabinetSerializer, ArchiveCategorySerializer, LedgerSerializer,
    ArchiveSerializer, BorrowRecordSerializer, LicenseSerializer,
    AnnualReviewSerializer, LicenseBorrowRecordSerializer,
    KnowledgeCategorySerializer, KnowledgeDocumentSerializer,
)


class DecorationViewSet(EnhancedModelViewSet):
    """装修记录 API"""
    perm_module = 'decoration'
    audit_label = '装修'
    audit_display_field = 'project_name'
    queryset = Decoration.objects.all()
    serializer_class = DecorationSerializer
    search_fields = ['project_name', 'constructor', 'project_address']
    ordering_fields = ['start_date', 'final_price', 'reduced_price']


class CustomerCreditViewSet(EnhancedModelViewSet):
    """客户账期 API"""
    perm_module = 'customer_credit'
    audit_label = '客户账期'
    audit_display_field = 'customer_name'
    queryset = CustomerCredit.objects.all()
    serializer_class = CustomerCreditSerializer
    search_fields = ['customer_name', 'salesperson', 'work_content', 'specific_content']
    ordering_fields = ['proposal_date', 'site_date']


class CustomerComplaintViewSet(EnhancedModelViewSet):
    """客户客诉 API"""
    perm_module = 'customer_complaint'
    audit_label = '客户客诉'
    audit_display_field = 'customer_name'
    queryset = CustomerComplaint.objects.all()
    serializer_class = CustomerComplaintSerializer
    search_fields = ['customer_name', 'responsible_salesperson', 'terminal_customer', 'product_model']
    ordering_fields = ['complaint_date', 'compensation_amount']


class RegulationViewSet(EnhancedModelViewSet):
    """制度及处罚 API"""
    perm_module = 'regulation'
    audit_label = '制度及处罚'
    audit_display_field = 'title'
    queryset = Regulation.objects.all()
    serializer_class = RegulationSerializer
    search_fields = ['title', 'content', 'doc_no']
    ordering_fields = ['issue_date']


class PriceEvaluationViewSet(EnhancedModelViewSet):
    """核价 API"""
    perm_module = 'price_evaluation'
    audit_label = '核价'
    audit_display_field = 'project_category'
    queryset = PriceEvaluation.objects.all()
    serializer_class = PriceEvaluationSerializer
    search_fields = ['project_category', 'proposing_department', 'proposer', 'evaluation_content']
    ordering_fields = ['internal_control_price']


class DailyRecordViewSet(EnhancedModelViewSet):
    """每日记录 API（数据隔离：仅返回/操作当前用户的记录）"""
    perm_module = 'daily_record'
    audit_label = '每日记录'
    audit_display_field = 'work_content'
    queryset = DailyRecord.objects.all()
    serializer_class = DailyRecordSerializer
    search_fields = ['department', 'work_content']
    ordering_fields = ['record_date', 'progress']

    def get_queryset(self):
        # 在父类（日期范围过滤等）基础上叠加用户归属过滤
        return super().get_queryset().filter(user=self.request.user)

    def perform_create(self, serializer):
        # 归属绑定当前用户，服务端强制，忽略前端提交的归属字段
        serializer.save(user=self.request.user)


class MetalPriceViewSet(EnhancedModelViewSet):
    """金属价格 API（registry 仅授予 read/update，故普通角色无法 create/destroy）"""
    perm_module = 'metal_price'
    audit_label = '金属价格'
    audit_display_field = 'metal_name'
    queryset = MetalPrice.objects.all()
    serializer_class = MetalPriceSerializer
    search_fields = ['metal_name', 'unit']
    ordering_fields = ['crawl_date', 'price', 'price_change']


class InsuranceViewSet(EnhancedModelViewSet):
    """保险 API"""
    perm_module = 'insurance'
    audit_label = '保险'
    audit_display_field = 'title'
    queryset = Insurance.objects.all()
    serializer_class = InsuranceSerializer
    search_fields = ['company_name', 'title', 'order_number', 'department']
    ordering_fields = ['issue_date', 'end_date', 'premium']


class ComplaintViewSet(EnhancedModelViewSet):
    """客诉 API"""
    perm_module = 'complaint'
    audit_label = '客诉'
    audit_display_field = 'case_number'
    queryset = Complaint.objects.all()
    serializer_class = ComplaintSerializer
    search_fields = ['dispute_type', 'defendant', 'case_number', 'court', 'lawyer']
    ordering_fields = ['amount', 'progress']


# ==================== 试样管理 ====================

class PartnerViewSet(EnhancedModelViewSet):
    """供应商档案 API"""
    perm_module = 'samples'
    audit_label = '试样管理'
    audit_object_name = '供应商档案'
    audit_display_field = 'name'
    audit_style = 'plain'
    queryset = Partner.objects.all()
    serializer_class = PartnerSerializer
    search_fields = ['partner_code', 'name', 'contact_person', 'contact_info']
    ordering_fields = ['created_at', 'name']


class SampleViewSet(EnhancedModelViewSet):
    """样品主档 API（不含已软删除样品；删除请走页面软删除流程）"""
    perm_module = 'samples'
    audit_label = '试样管理'
    audit_object_name = '样品主档'
    audit_display_field = 'sample_no'
    audit_style = 'plain'
    queryset = Sample.objects.filter(is_deleted=False)
    serializer_class = SampleSerializer
    search_fields = ['sample_no', 'material_name', 'model', 'spec', 'registrar']
    ordering_fields = ['sample_date', 'status']


class TestRecordViewSet(EnhancedModelViewSet):
    """试样记录 API"""
    perm_module = 'samples'
    audit_label = '试样管理'
    audit_object_name = '试样记录'
    audit_display_field = 'test_no'
    audit_style = 'plain'
    queryset = TestRecord.objects.all()
    serializer_class = TestRecordSerializer
    search_fields = ['test_no', 'test_result', 'report_code', 'operator', 'package_type']
    ordering_fields = ['test_date', 'round_no']


class BatchRecordViewSet(EnhancedModelViewSet):
    """小批量记录 API"""
    perm_module = 'samples'
    audit_label = '试样管理'
    audit_object_name = '小批量记录'
    audit_display_field = 'batch_no'
    audit_style = 'plain'
    queryset = BatchRecord.objects.all()
    serializer_class = BatchRecordSerializer
    search_fields = ['batch_no', 'alternative_model', 'operator', 'remark']
    ordering_fields = ['round_no']


class AuditRecordViewSet(EnhancedModelViewSet):
    """审厂记录 API（附件上传走页面）"""
    perm_module = 'samples'
    audit_label = '试样管理'
    audit_object_name = '审厂记录'
    audit_display_field = 'audit_no'
    audit_style = 'plain'
    queryset = AuditRecord.objects.all()
    serializer_class = AuditRecordSerializer
    search_fields = ['audit_no', 'audit_remark', 'auditor']
    ordering_fields = ['audit_date', 'round_no']


# ==================== 档案管理 ====================

class CabinetViewSet(EnhancedModelViewSet):
    """档案柜 API（层格配置请走页面，AI 创建后需补充层配置）"""
    perm_module = 'archive'
    audit_label = '档案管理'
    audit_object_name = '档案柜'
    audit_display_field = 'code'
    audit_style = 'plain'
    queryset = Cabinet.objects.all()
    serializer_class = CabinetSerializer
    search_fields = ['code', 'location']
    ordering_fields = ['code']


class ArchiveCategoryViewSet(EnhancedModelViewSet):
    """档案类别 API"""
    perm_module = 'archive'
    audit_label = '档案管理'
    audit_object_name = '类别'
    audit_display_field = 'name'
    audit_style = 'plain'
    queryset = ArchiveCategory.objects.all()
    serializer_class = ArchiveCategorySerializer
    search_fields = ['name']
    ordering_fields = ['sort_order']


class LedgerViewSet(EnhancedModelViewSet):
    """台账 API"""
    perm_module = 'archive'
    audit_label = '档案管理'
    audit_object_name = '台账'
    audit_display_field = 'name'
    audit_style = 'plain'
    queryset = Ledger.objects.all()
    serializer_class = LedgerSerializer
    search_fields = ['name', 'description']
    ordering_fields = ['sort_order']


class ArchiveViewSet(EnhancedModelViewSet):
    """档案 API"""
    perm_module = 'archive'
    audit_label = '档案管理'
    audit_object_name = '档案'
    audit_display_field = 'code'
    audit_style = 'plain'
    queryset = Archive.objects.all()
    serializer_class = ArchiveSerializer
    search_fields = [
        'code', 'name', 'tags', 'description', 'counterpart', 'contract_no',
        'department', 'archive_label', 'handler', 'location_code',
    ]
    ordering_fields = ['created_at', 'sign_date', 'expire_date']


class BorrowRecordViewSet(ReadOnlyEnhancedViewSet):
    """档案借阅记录 API（借阅/归还操作走页面业务流程）"""
    perm_module = 'archive'
    queryset = BorrowRecord.objects.all()
    serializer_class = BorrowRecordSerializer
    search_fields = ['borrower', 'remark', 'archive__code', 'archive__name']
    ordering_fields = ['borrow_time', 'due_time']


class LicenseViewSet(EnhancedModelViewSet):
    """证照 API"""
    perm_module = 'archive'
    audit_label = '档案管理'
    audit_object_name = '证照'
    audit_display_field = 'name'
    audit_style = 'plain'
    queryset = License.objects.all()
    serializer_class = LicenseSerializer
    search_fields = ['name', 'code', 'holder', 'issuer', 'remark']
    ordering_fields = ['expire_date', 'next_review_date']


class AnnualReviewViewSet(ReadOnlyEnhancedViewSet):
    """年审记录 API（年审操作走页面业务流程）"""
    perm_module = 'archive'
    queryset = AnnualReview.objects.all()
    serializer_class = AnnualReviewSerializer
    search_fields = ['reviewer', 'remark', 'license__code', 'license__name']
    ordering_fields = ['review_date']


class LicenseBorrowRecordViewSet(ReadOnlyEnhancedViewSet):
    """证照借阅记录 API（借阅/归还操作走页面业务流程）"""
    perm_module = 'archive'
    queryset = LicenseBorrowRecord.objects.all()
    serializer_class = LicenseBorrowRecordSerializer
    search_fields = ['borrower', 'remark', 'license__code', 'license__name']
    ordering_fields = ['borrow_time', 'due_time']


# ==================== 知识库 ====================

class KnowledgeCategoryViewSet(EnhancedModelViewSet):
    """知识分类 API"""
    perm_module = 'knowledge'
    audit_label = '知识库'
    audit_object_name = '知识分类'
    audit_display_field = 'name'
    audit_style = 'plain'
    queryset = KnowledgeCategory.objects.all()
    serializer_class = KnowledgeCategorySerializer
    search_fields = ['name', 'description']
    ordering_fields = ['sort_order']


class KnowledgeDocumentViewSet(EnhancedModelViewSet):
    """知识文档 API（可见性过滤：公开 + 本人文档，走 access 唯一入口）

    不改这里，页面侧 1~7 处读路径全部白改：任何有 knowledge.read 的用户
    走本接口可分页枚举所有人的私有文档标题+正文（serializer 含 content）。
    """
    perm_module = 'knowledge'
    audit_label = '知识库'
    audit_object_name = '知识文档'
    audit_display_field = 'title'
    audit_style = 'plain'
    queryset = KnowledgeDocument.objects.all()
    serializer_class = KnowledgeDocumentSerializer
    search_fields = ['title', 'content', 'summary', 'tags']
    ordering_fields = ['updated_at', 'view_count']

    def get_queryset(self):
        from knowledge_base import access as kb_access
        return kb_access.visible_docs(self.request.user, KnowledgeDocument.objects.all())


# ========== AI 友好接口 ==========

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def ai_summary(request):
    """AI 数据总结接口"""
    summary = {
        '总数据统计': {},
        '今日数据': {},
        '即将到期': {},
        '待处理': {}
    }
    
    # 总数据统计（仅聚合当前用户有 read 权限的模块）
    totals = {}
    if _can(request.user, 'decoration', 'read'):
        totals['装修记录'] = Decoration.objects.count()
    if _can(request.user, 'customer_credit', 'read'):
        totals['客户账期'] = CustomerCredit.objects.count()
    if _can(request.user, 'customer_complaint', 'read'):
        totals['客户客诉'] = CustomerComplaint.objects.count()
    if _can(request.user, 'regulation', 'read'):
        totals['制度及处罚'] = Regulation.objects.count()
    if _can(request.user, 'price_evaluation', 'read'):
        totals['核价'] = PriceEvaluation.objects.count()
    if _can(request.user, 'daily_record', 'read'):
        totals['每日记录'] = DailyRecord.objects.filter(user=request.user).count()
    if _can(request.user, 'metal_price', 'read'):
        totals['金属价格'] = MetalPrice.objects.count()
    if _can(request.user, 'insurance', 'read'):
        totals['保险'] = Insurance.objects.count()
    if _can(request.user, 'complaint', 'read'):
        totals['客诉'] = Complaint.objects.count()
    if _can(request.user, 'samples', 'read'):
        totals['供应商档案'] = Partner.objects.count()
        totals['样品主档'] = Sample.objects.filter(is_deleted=False).count()
        totals['试样记录'] = TestRecord.objects.count()
        totals['小批量记录'] = BatchRecord.objects.count()
        totals['审厂记录'] = AuditRecord.objects.count()
    if _can(request.user, 'archive', 'read'):
        totals['档案'] = Archive.objects.count()
        totals['证照'] = License.objects.count()
    if _can(request.user, 'knowledge', 'read'):
        # 可见性口径：只统计公开 + 本人文档，不泄漏他人私有文档的规模
        from knowledge_base import access as kb_access
        totals['知识文档'] = kb_access.visible_docs(request.user).count()
    summary['总数据统计'] = totals

    # 今日数据
    today = datetime.now().date()
    today_data = {}
    if _can(request.user, 'daily_record', 'read'):
        today_data['今日新增记录'] = DailyRecord.objects.filter(
            user=request.user,
            record_date__date=today
        ).count()
    if _can(request.user, 'metal_price', 'read'):
        today_data['今日金属价格'] = MetalPrice.objects.filter(
            crawl_date=today
        ).count()
    if _can(request.user, 'samples', 'read'):
        today_data['今日送样'] = Sample.objects.filter(
            sample_date=today, is_deleted=False
        ).count()
    if _can(request.user, 'archive', 'read'):
        today_data['今日档案借出'] = BorrowRecord.objects.filter(
            borrow_time__date=today
        ).count()
    summary['今日数据'] = today_data

    # 即将到期（保险）
    if _can(request.user, 'insurance', 'read'):
        upcoming_insurance = Insurance.objects.filter(
            end_date__lte=today + timedelta(days=30),
            end_date__gte=today
        )
        summary['即将到期']['保险(30天内)'] = upcoming_insurance.count()
        summary['即将到期']['保险列表'] = InsuranceSerializer(upcoming_insurance[:5], many=True).data

    # 即将到期（证照/档案合同）
    if _can(request.user, 'archive', 'read'):
        summary['即将到期']['证照(30天内)'] = License.objects.filter(
            Q(expire_date__gte=today, expire_date__lte=today + timedelta(days=30))
            | Q(next_review_date__gte=today, next_review_date__lte=today + timedelta(days=30))
        ).count()
        summary['即将到期']['档案合同(30天内)'] = Archive.objects.filter(
            expire_date__gte=today, expire_date__lte=today + timedelta(days=30)
        ).count()

    # 待处理（未完成的客诉和账期）
    pending = {}
    if _can(request.user, 'customer_complaint', 'read'):
        pending['未完成客户客诉'] = CustomerComplaint.objects.filter(
            complaint_status='未完成'
        ).count()
        pending['处理中客户客诉'] = CustomerComplaint.objects.filter(
            complaint_status='处理中'
        ).count()
    if _can(request.user, 'complaint', 'read'):
        pending['未完成客诉'] = Complaint.objects.filter(
            progress__in=['立案', '审理中']
        ).count()
    if _can(request.user, 'samples', 'read'):
        pending['待送样样品'] = Sample.objects.filter(
            status=Sample.Status.PENDING, is_deleted=False
        ).count()
        pending['试样中样品'] = Sample.objects.filter(
            status=Sample.Status.TESTING, is_deleted=False
        ).count()
    if _can(request.user, 'archive', 'read'):
        pending['借出中档案'] = BorrowRecord.objects.filter(return_time__isnull=True).count()
        pending['借出中证照'] = LicenseBorrowRecord.objects.filter(return_time__isnull=True).count()
    summary['待处理'] = pending

    return Response(summary)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def ai_search(request):
    """AI 搜索查询接口"""
    query = request.GET.get('q', '')
    model = request.GET.get('model', '')

    if not query:
        return Response({'error': '请提供搜索关键词'}, status=400)

    results = {}

    # 知识库可见性过滤入口（供 models_to_search 使用）
    from knowledge_base import access as kb_access

    # 根据 model 参数搜索指定模型，或搜索所有模型
    # 值为 (model, serializer[, base_queryset])；base_queryset 缺省为全表
    models_to_search = {
        'decoration': (Decoration, DecorationSerializer),
        'customer_credit': (CustomerCredit, CustomerCreditSerializer),
        'customer_complaint': (CustomerComplaint, CustomerComplaintSerializer),
        'regulation': (Regulation, RegulationSerializer),
        'price_evaluation': (PriceEvaluation, PriceEvaluationSerializer),
        'daily_record': (DailyRecord, DailyRecordSerializer),
        'insurance': (Insurance, InsuranceSerializer),
        'complaint': (Complaint, ComplaintSerializer),
        'partner': (Partner, PartnerSerializer),
        'sample': (Sample, SampleSerializer, Sample.objects.filter(is_deleted=False)),
        'test_record': (TestRecord, TestRecordSerializer),
        'batch_record': (BatchRecord, BatchRecordSerializer),
        'audit_record': (AuditRecord, AuditRecordSerializer),
        'archive': (Archive, ArchiveSerializer),
        'license': (License, LicenseSerializer),
        # 知识文档按可见性过滤（公开 + 本人文档），防止私有文档经搜索通道外泄
        'knowledge_document': (
            KnowledgeDocument, KnowledgeDocumentSerializer,
            kb_access.visible_docs(request.user),
        ),
    }

    def _text_search(model_class, serializer_class, base_queryset, limit):
        """对模型全部文本字段做 OR 模糊匹配，返回序列化结果。"""
        text_fields = [f.name for f in model_class._meta.get_fields()
                       if hasattr(f, 'name') and f.name != 'id'
                       and getattr(f, 'get_internal_type', lambda: '')() in ('CharField', 'TextField')]
        q_filter = Q()
        for field in text_fields:
            q_filter |= Q(**{f'{field}__icontains': query})
        return serializer_class(
            base_queryset.filter(q_filter).distinct()[:limit], many=True
        ).data

    if model and model in models_to_search:
        # 指定模块但无 read 权限：403（不得借搜索通道越权读取）
        if not _can(request.user, _ai_perm_module(model), 'read'):
            return Response({'detail': f'无权访问模块 {model}'}, status=403)
        entry = models_to_search[model]
        model_class, serializer_class = entry[0], entry[1]
        base_queryset = entry[2] if len(entry) > 2 else model_class.objects.all()
        results[model] = _text_search(model_class, serializer_class, base_queryset, 20)
    else:
        # 搜索所有有权限的模型
        for name, entry in models_to_search.items():
            if not _can(request.user, _ai_perm_module(name), 'read'):
                continue
            model_class, serializer_class = entry[0], entry[1]
            base_queryset = entry[2] if len(entry) > 2 else model_class.objects.all()
            data = _text_search(model_class, serializer_class, base_queryset, 10)
            if data:
                results[name] = data

    return Response(results)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def ai_query(request):
    """AI 智能问答接口 - 支持自然语言查询"""
    question = request.GET.get('question', '')

    if not question:
        return Response({'error': '请提供问题'}, status=400)

    response = {'问题': question, '答案': None, '数据': None}

    # 关键词→模型映射表（支持同义词）
    keyword_map = {
        '保险': {'model': 'insurance', 'keywords': ['保险', '保单', '保费', '到期', '续保']},
        '客诉': {'model': 'customer_complaint', 'keywords': ['客诉', '客户投诉', '投诉', '客诉处理']},
        '诉讼': {'model': 'complaint', 'keywords': ['诉讼', '纠纷', '案号', '被告', '法院']},
        '账期': {'model': 'customer_credit', 'keywords': ['账期', '应收账款', '欠款', '回款', '客户账期']},
        '记录': {'model': 'daily_record', 'keywords': ['记录', '工作', '任务', '未完成', '今日工作']},
        '装修': {'model': 'decoration', 'keywords': ['装修', '施工', '核减', '造价']},
        '核价': {'model': 'price_evaluation', 'keywords': ['核价', '供应商', '报价', '内控']},
        '金属': {'model': 'metal_price', 'keywords': ['金属', '铜价', '锡价', '铝价', '金价', '银价', '价格', '涨跌']},
        '制度': {'model': 'regulation', 'keywords': ['制度', '处罚', '发文']},
        '试样': {'model': 'sample', 'keywords': ['试样', '送样', '样品', '试产', '小批量']},
        '供应商': {'model': 'partner', 'keywords': ['供应商档案', '审厂', '合作厂商']},
        '档案': {'model': 'archive', 'keywords': ['档案', '台账', '归档', '借阅']},
        '证照': {'model': 'license', 'keywords': ['证照', '证书', '资质', '营业执照', '年审']},
        '知识': {'model': 'knowledge_document', 'keywords': ['知识库', '知识文档', '文档', '知识']},
    }

    # 扩展词匹配：支持"铜价"、"金价"等变体
    expanded_map = {
        '铜': '金属', '锡': '金属', '铝': '金属', '锌': '金属',
        '铅': '金属', '镍': '金属', '金': '金属', '银': '金属',
    }

    def match_score(q, keywords):
        """问题与主题的匹配得分 = 命中关键词总长度 / 问题长度。

        改造前是"每命中一个关键词就累加该词长度"，于是词表里词越长越容易排前面，
        排序实际取决于分词表而不是相关度（审计 #35）。归一化后分数落在 0~1，
        同一问题在不同主题间的比较才有意义。
        同一关键词不再因大小写两次命中而重复计分。
        """
        if not q:
            return 0.0
        q_lower = q.lower()
        matched = {kw for kw in keywords if kw and kw.lower() in q_lower}
        if not matched:
            return 0.0
        return sum(len(kw) for kw in matched) / len(q)

    # 找出最匹配的主题
    best_match = None
    best_score = 0

    for key, info in keyword_map.items():
        score = match_score(question, info['keywords'])
        if score > best_score:
            best_score = score
            best_match = (key, info)

    # 检查扩展词（如"铜价" → "金属"）
    for short, full in expanded_map.items():
        if short in question and full in keyword_map:
            full_info = keyword_map[full]
            score = match_score(question, full_info['keywords'])
            if score > best_score:
                best_score = score
                best_match = (full, full_info)

    if best_match:
        key, info = best_match
        model_name = info['model']

        # 无该模块 read 权限：不返回任何业务数据
        if not _can(request.user, _ai_perm_module(model_name), 'read'):
            response['答案'] = f'当前账号无权访问「{key}」模块数据，请联系管理员开通权限。'
            return Response(response)

        if model_name == 'insurance':
            if any(kw in question for kw in ['到期', '过期']):
                expired = Insurance.objects.filter(end_date__lt=datetime.now().date())
                upcoming = Insurance.objects.filter(
                    end_date__gte=datetime.now().date(),
                    end_date__lte=datetime.now().date() + timedelta(days=30)
                )
                response['答案'] = f'已到期{expired.count()}个，30天内到期{upcoming.count()}个'
                response['数据'] = InsuranceSerializer(expired[:5], many=True).data
            else:
                response['数据'] = InsuranceSerializer(Insurance.objects.all()[:10], many=True).data
                response['答案'] = f'共{Insurance.objects.count()}条保险记录'

        elif model_name == 'customer_complaint':
            if any(kw in question for kw in ['未完成', '处理中']):
                data = CustomerComplaint.objects.filter(complaint_status__in=['未完成', '处理中'])
                response['答案'] = f'有{data.count()}个待处理客诉'
            else:
                data = CustomerComplaint.objects.all()
            response['数据'] = CustomerComplaintSerializer(data[:10], many=True).data

        elif model_name == 'complaint':
            data = Complaint.objects.exclude(progress='结案')
            response['答案'] = f'共{len(data)}条未结案诉讼'

        elif model_name == 'customer_credit':
            response['数据'] = CustomerCreditSerializer(CustomerCredit.objects.all()[:10], many=True).data
            response['答案'] = f'共{CustomerCredit.objects.count()}条账期记录'

        elif model_name == 'daily_record':
            # 数据隔离：AI 问答仅引用当前用户的记录
            my_records = DailyRecord.objects.filter(user=request.user)
            recent = my_records[:20]
            response['数据'] = DailyRecordSerializer(recent, many=True).data
            response['答案'] = f'共{my_records.count()}条工作记录'

        elif model_name == 'decoration':
            response['数据'] = DecorationSerializer(Decoration.objects.all()[:10], many=True).data
            response['答案'] = f'共{Decoration.objects.count()}条装修记录'

        elif model_name == 'price_evaluation':
            response['数据'] = PriceEvaluationSerializer(PriceEvaluation.objects.all()[:10], many=True).data
            response['答案'] = f'共{PriceEvaluation.objects.count()}条核价记录'

        elif model_name == 'metal_price':
            latest = MetalPrice.objects.all()[:15]
            response['数据'] = MetalPriceSerializer(latest, many=True).data
            response['答案'] = f'共{MetalPrice.objects.count()}条价格记录'

        elif model_name == 'regulation':
            response['数据'] = RegulationSerializer(Regulation.objects.all()[:10], many=True).data
            response['答案'] = f'共{Regulation.objects.count()}条制度记录'

        elif model_name == 'sample':
            active = Sample.objects.filter(is_deleted=False)
            status_counts = {
                s: active.filter(status=s).count()
                for s, _label in Sample.Status.choices
            }
            response['数据'] = SampleSerializer(active[:10], many=True).data
            response['答案'] = (
                f'共{active.count()}个样品：待送样{status_counts["pending"]}、'
                f'试样中{status_counts["testing"]}、小批量中{status_counts["small_batch"]}、'
                f'已导入{status_counts["imported"]}、已终止{status_counts["terminated"]}；'
                f'另有试样记录{TestRecord.objects.count()}条、小批量记录{BatchRecord.objects.count()}条'
            )

        elif model_name == 'partner':
            response['数据'] = PartnerSerializer(Partner.objects.all()[:10], many=True).data
            response['答案'] = (
                f'共{Partner.objects.count()}家供应商档案，'
                f'合作中{Partner.objects.filter(status=Partner.CooperationStatus.ACTIVE).count()}家'
            )

        elif model_name == 'archive':
            response['数据'] = ArchiveSerializer(Archive.objects.all()[:10], many=True).data
            response['答案'] = (
                f'共{Archive.objects.count()}份档案，'
                f'借出中{BorrowRecord.objects.filter(return_time__isnull=True).count()}份'
            )

        elif model_name == 'license':
            today = datetime.now().date()
            if any(kw in question for kw in ['到期', '过期', '年审']):
                expired = License.objects.filter(
                    Q(expire_date__lt=today) | Q(next_review_date__lt=today)
                ).count()
                upcoming = License.objects.filter(
                    Q(expire_date__gte=today, expire_date__lte=today + timedelta(days=30))
                    | Q(next_review_date__gte=today, next_review_date__lte=today + timedelta(days=30))
                ).count()
                response['答案'] = f'已过期{expired}个，30天内到期/年审{upcoming}个'
            else:
                response['答案'] = f'共{License.objects.count()}份证照'
            response['数据'] = LicenseSerializer(License.objects.all()[:10], many=True).data

        elif model_name == 'knowledge_document':
            # 可见性口径：只返回公开 + 本人文档，私有文档正文不得经 AI 问答通道外泄
            from knowledge_base import access as kb_access
            visible_knowledge = kb_access.visible_docs(request.user)
            response['数据'] = KnowledgeDocumentSerializer(
                visible_knowledge[:10], many=True
            ).data
            response['答案'] = f'共{visible_knowledge.count()}篇知识文档'
    else:
        response['答案'] = (
            '请明确搜索内容，支持：保险、客诉、诉讼、账期、记录、装修、核价、金属、'
            '制度、试样、供应商、档案、证照、知识'
        )

    return Response(response)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def model_list(request):
    """获取所有可用模型列表"""
    from knowledge_base import access as kb_access
    models = {
        'decoration': {
            'name': '装修记录',
            'count': Decoration.objects.count(),
            'fields': [f.name for f in Decoration._meta.get_fields() if hasattr(f, 'name')]
        },
        'customer_credit': {
            'name': '客户账期',
            'count': CustomerCredit.objects.count(),
            'fields': [f.name for f in CustomerCredit._meta.get_fields() if hasattr(f, 'name')]
        },
        'customer_complaint': {
            'name': '客户客诉',
            'count': CustomerComplaint.objects.count(),
            'fields': [f.name for f in CustomerComplaint._meta.get_fields() if hasattr(f, 'name')]
        },
        'regulation': {
            'name': '制度及处罚',
            'count': Regulation.objects.count(),
            'fields': [f.name for f in Regulation._meta.get_fields() if hasattr(f, 'name')]
        },
        'price_evaluation': {
            'name': '核价',
            'count': PriceEvaluation.objects.count(),
            'fields': [f.name for f in PriceEvaluation._meta.get_fields() if hasattr(f, 'name')]
        },
        'daily_record': {
            'name': '每日记录',
            'count': DailyRecord.objects.filter(user=request.user).count(),
            'fields': [f.name for f in DailyRecord._meta.get_fields() if hasattr(f, 'name')]
        },
        'metal_price': {
            'name': '金属价格',
            'count': MetalPrice.objects.count(),
            'fields': [f.name for f in MetalPrice._meta.get_fields() if hasattr(f, 'name')]
        },
        'insurance': {
            'name': '保险',
            'count': Insurance.objects.count(),
            'fields': [f.name for f in Insurance._meta.get_fields() if hasattr(f, 'name')]
        },
        'complaint': {
            'name': '客诉',
            'count': Complaint.objects.count(),
            'fields': [f.name for f in Complaint._meta.get_fields() if hasattr(f, 'name')]
        },
        'partner': {
            'name': '供应商档案',
            'count': Partner.objects.count(),
            'fields': [f.name for f in Partner._meta.get_fields() if hasattr(f, 'name')]
        },
        'sample': {
            'name': '样品主档',
            'count': Sample.objects.filter(is_deleted=False).count(),
            'fields': [f.name for f in Sample._meta.get_fields() if hasattr(f, 'name')]
        },
        'test_record': {
            'name': '试样记录',
            'count': TestRecord.objects.count(),
            'fields': [f.name for f in TestRecord._meta.get_fields() if hasattr(f, 'name')]
        },
        'batch_record': {
            'name': '小批量记录',
            'count': BatchRecord.objects.count(),
            'fields': [f.name for f in BatchRecord._meta.get_fields() if hasattr(f, 'name')]
        },
        'audit_record': {
            'name': '审厂记录',
            'count': AuditRecord.objects.count(),
            'fields': [f.name for f in AuditRecord._meta.get_fields() if hasattr(f, 'name')]
        },
        'archive': {
            'name': '档案',
            'count': Archive.objects.count(),
            'fields': [f.name for f in Archive._meta.get_fields() if hasattr(f, 'name')]
        },
        'license': {
            'name': '证照',
            'count': License.objects.count(),
            'fields': [f.name for f in License._meta.get_fields() if hasattr(f, 'name')]
        },
        'knowledge_document': {
            'name': '知识文档',
            # 可见性口径：文档总数不泄漏他人私有文档的规模
            'count': kb_access.visible_docs(request.user).count(),
            'fields': [f.name for f in KnowledgeDocument._meta.get_fields() if hasattr(f, 'name')]
        },
    }
    # 仅返回当前用户有 read 权限的模块
    return Response({k: v for k, v in models.items() if _can(request.user, _ai_perm_module(k), 'read')})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def ai_create(request):
    """AI 创建数据接口

    models_map 值为 (model, serializer[, service])：
    - 带 service 时复用 samples.services 的创建服务（自动生成单据编号、
      派生样品状态并写操作日志）；service 内 full_clean 失败返回 400。
    - audit_record / 借阅 / 年审等涉及文件上传或页面流程的模型不开放 AI 创建。
    """
    data = request.data
    model_type = data.get('model')

    models_map = {
        'decoration': (Decoration, DecorationSerializer),
        'customer_credit': (CustomerCredit, CustomerCreditSerializer),
        'customer_complaint': (CustomerComplaint, CustomerComplaintSerializer),
        'regulation': (Regulation, RegulationSerializer),
        'price_evaluation': (PriceEvaluation, PriceEvaluationSerializer),
        'daily_record': (DailyRecord, DailyRecordSerializer),
        'insurance': (Insurance, InsuranceSerializer),
        'complaint': (Complaint, ComplaintSerializer),
        'partner': (Partner, PartnerSerializer, create_partner),
        'sample': (Sample, SampleSerializer, create_sample),
        'test_record': (TestRecord, TestRecordSerializer, create_test_record),
        'batch_record': (BatchRecord, BatchRecordSerializer, create_batch_record),
        'cabinet': (Cabinet, CabinetSerializer),
        'archive_category': (ArchiveCategory, ArchiveCategorySerializer),
        'ledger': (Ledger, LedgerSerializer),
        'archive': (Archive, ArchiveSerializer),
        'license': (License, LicenseSerializer),
        'knowledge_category': (KnowledgeCategory, KnowledgeCategorySerializer),
        'knowledge_document': (KnowledgeDocument, KnowledgeDocumentSerializer),
    }

    if model_type not in models_map:
        return Response({'error': '无效的模型类型'}, status=400)

    # RBAC：AI 通道与页面表单同一把锁，无对应模块 create 权限一律 403
    if not _can(request.user, _ai_perm_module(model_type), 'create'):
        return Response({'detail': f'无权在模块 {model_type} 新增数据'}, status=403)

    entry = models_map[model_type]
    model_class, serializer_class = entry[0], entry[1]
    service = entry[2] if len(entry) > 2 else None
    serializer = serializer_class(data=data.get('data', {}))

    if not serializer.is_valid():
        return Response(serializer.errors, status=400)

    if service:
        obj = model_class(**serializer.validated_data)
        try:
            obj = service(obj, operator=request.user.username)
        except (DjangoValidationError, BusinessError) as exc:
            message = getattr(exc, 'message_dict', None) or {'detail': [str(exc)]}
            return Response(message, status=400)
        return Response(serializer_class(obj).data, status=201)

    if model_type == 'daily_record':
        # DailyRecord.user 为非空外键且序列化器只读：必须绑定当前用户，否则触发 IntegrityError 500
        serializer.save(user=request.user)
    else:
        serializer.save()
    return Response(serializer.data, status=201)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def health_check(request):
    """健康检查：做真实探测，而不是恒定返回 ok。

    改造前固定返回 {'status': 'ok', 'ai_ready': True}——一个永远绿的检查等于
    没有检查（审计 #23）。现在逐项探测：数据库、未应用迁移、日志目录可写。
    任一项不通过 => status=degraded + HTTP 503（便于探针直接判死）。

    探测逻辑已迁移至 work_record.health.probe_health()，供系统健康面板共用；
    本视图只负责组装 payload 与状态码，对外契约保持完全不变。
    """
    from work_record.health import probe_health

    checks, healthy = probe_health()
    payload = {
        'status': 'ok' if healthy else 'degraded',
        'service': 'AI_daily_work_record',
        'version': '1.0.0',
        # 旧的键保留（调用方可能已经在读），值改为真实探测结果
        'ai_ready': False,
        'checks': checks,
        'timestamp': timezone.now().isoformat(),
    }
    return Response(payload, status=200 if healthy else 503)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_background_images(request):
    """动态获取背景图片列表"""
    import os
    import re
    from django.conf import settings
    
    # 开发环境使用 STATICFILES_DIRS，生产环境使用 STATIC_ROOT
    if settings.STATIC_ROOT and os.path.exists(settings.STATIC_ROOT):
        static_dir = settings.STATIC_ROOT
    elif settings.STATICFILES_DIRS:
        static_dir = settings.STATICFILES_DIRS[0]
    else:
        return Response({
            'success': False,
            'error': '未找到静态文件目录'
        })
    
    images_dir = os.path.join(static_dir, 'images')
    
    # 支持的图片格式
    allowed_extensions = ('.png', '.jpg', '.jpeg', '.gif', '.webp')
    
    # 查找所有背景图片（以 background 开头）
    background_images = []
    if os.path.exists(images_dir):
        for filename in sorted(os.listdir(images_dir)):
            # 只处理以 background 开头的图片文件
            if filename.startswith('background') and filename.lower().endswith(allowed_extensions):
                # 构建 URL 路径
                image_url = f'/static/images/{filename}'
                
                # 提取索引号：使用正则表达式提取 background 后面的数字
                match = re.match(r'background(\d+)', filename)
                if match:
                    index = int(match.group(1))
                else:
                    index = len(background_images)
                
                background_images.append({
                    'name': filename,
                    'url': image_url,
                    'index': index
                })
    
    # 按 index 排序
    background_images.sort(key=lambda x: x['index'])
    
    return Response({
        'success': True,
        'count': len(background_images),
        'images': [img['url'] for img in background_images]
    })