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


from .viewsets import _can, _ai_perm_module  # 复用基类与权限映射，避免重复实现

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

    def _owner_scoped(model_class, base_queryset):
        """对含 user 归属字段的模型追加本人过滤，防止全局检索通道越权读取他人数据。

        审计 P1：daily_record 未在入口处过滤 user，任何登录用户都能通过
        /api/ai/search/?q= 检索到全站所有人的每日记录。
        """
        user_field = next(
            (f for f in model_class._meta.get_fields()
             if getattr(f, 'name', '') == 'user' and getattr(f, 'is_relation', False)),
            None,
        )
        if user_field is not None:
            return base_queryset.filter(user=request.user)
        return base_queryset

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
        base_queryset = _owner_scoped(model_class, base_queryset)
        results[model] = _text_search(model_class, serializer_class, base_queryset, 20)
    else:
        # 搜索所有有权限的模型
        for name, entry in models_to_search.items():
            if not _can(request.user, _ai_perm_module(name), 'read'):
                continue
            model_class, serializer_class = entry[0], entry[1]
            base_queryset = entry[2] if len(entry) > 2 else model_class.objects.all()
            base_queryset = _owner_scoped(model_class, base_queryset)
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
