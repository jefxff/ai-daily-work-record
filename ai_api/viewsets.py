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


