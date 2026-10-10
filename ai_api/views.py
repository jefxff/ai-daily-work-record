from rest_framework import viewsets, filters
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination
from django.db.models import Q
from datetime import datetime, timedelta

from accounts.services import has_perm


def _can(user, module, action):
    """AI 函数视图的 RBAC 判定（超管在 has_perm 内全部放行）。"""
    return has_perm(user, module, action)


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

from records.models import (
    Decoration, CustomerCredit, CustomerComplaint, Regulation,
    PriceEvaluation, DailyRecord, MetalPrice, Insurance, Complaint
)
from .serializers import (
    DecorationSerializer, CustomerCreditSerializer, CustomerComplaintSerializer,
    RegulationSerializer, PriceEvaluationSerializer, DailyRecordSerializer,
    MetalPriceSerializer, InsuranceSerializer, ComplaintSerializer
)


class DecorationViewSet(EnhancedModelViewSet):
    """装修记录 API"""
    perm_module = 'decoration'
    queryset = Decoration.objects.all()
    serializer_class = DecorationSerializer
    search_fields = ['project_name', 'constructor', 'project_address']
    ordering_fields = ['start_date', 'final_price', 'reduced_price']


class CustomerCreditViewSet(EnhancedModelViewSet):
    """客户账期 API"""
    perm_module = 'customer_credit'
    queryset = CustomerCredit.objects.all()
    serializer_class = CustomerCreditSerializer
    search_fields = ['customer_name', 'salesperson', 'work_content', 'specific_content']
    ordering_fields = ['proposal_date', 'site_date']


class CustomerComplaintViewSet(EnhancedModelViewSet):
    """客户客诉 API"""
    perm_module = 'customer_complaint'
    queryset = CustomerComplaint.objects.all()
    serializer_class = CustomerComplaintSerializer
    search_fields = ['customer_name', 'responsible_salesperson', 'terminal_customer', 'product_model']
    ordering_fields = ['complaint_date', 'compensation_amount']


class RegulationViewSet(EnhancedModelViewSet):
    """制度及处罚 API"""
    perm_module = 'regulation'
    queryset = Regulation.objects.all()
    serializer_class = RegulationSerializer
    search_fields = ['title', 'content', 'doc_no']
    ordering_fields = ['issue_date']


class PriceEvaluationViewSet(EnhancedModelViewSet):
    """核价 API"""
    perm_module = 'price_evaluation'
    queryset = PriceEvaluation.objects.all()
    serializer_class = PriceEvaluationSerializer
    search_fields = ['project_category', 'proposing_department', 'proposer', 'evaluation_content']
    ordering_fields = ['internal_control_price']


class DailyRecordViewSet(EnhancedModelViewSet):
    """每日记录 API"""
    perm_module = 'daily_record'
    queryset = DailyRecord.objects.all()
    serializer_class = DailyRecordSerializer
    search_fields = ['department', 'work_content']
    ordering_fields = ['record_date', 'progress']


class MetalPriceViewSet(EnhancedModelViewSet):
    """金属价格 API（registry 仅授予 read/update，故普通角色无法 create/destroy）"""
    perm_module = 'metal_price'
    queryset = MetalPrice.objects.all()
    serializer_class = MetalPriceSerializer
    search_fields = ['metal_name', 'unit']
    ordering_fields = ['crawl_date', 'price', 'price_change']


class InsuranceViewSet(EnhancedModelViewSet):
    """保险 API"""
    perm_module = 'insurance'
    queryset = Insurance.objects.all()
    serializer_class = InsuranceSerializer
    search_fields = ['company_name', 'title', 'order_number', 'department']
    ordering_fields = ['issue_date', 'end_date', 'premium']


class ComplaintViewSet(EnhancedModelViewSet):
    """客诉 API"""
    perm_module = 'complaint'
    queryset = Complaint.objects.all()
    serializer_class = ComplaintSerializer
    search_fields = ['dispute_type', 'defendant', 'case_number', 'court', 'lawyer']
    ordering_fields = ['amount', 'progress']


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
        totals['每日记录'] = DailyRecord.objects.count()
    if _can(request.user, 'metal_price', 'read'):
        totals['金属价格'] = MetalPrice.objects.count()
    if _can(request.user, 'insurance', 'read'):
        totals['保险'] = Insurance.objects.count()
    if _can(request.user, 'complaint', 'read'):
        totals['客诉'] = Complaint.objects.count()
    summary['总数据统计'] = totals

    # 今日数据
    today = datetime.now().date()
    today_data = {}
    if _can(request.user, 'daily_record', 'read'):
        today_data['今日新增记录'] = DailyRecord.objects.filter(
            record_date__date=today
        ).count()
    if _can(request.user, 'metal_price', 'read'):
        today_data['今日金属价格'] = MetalPrice.objects.filter(
            crawl_date=today
        ).count()
    summary['今日数据'] = today_data

    # 即将到期（保险）
    if _can(request.user, 'insurance', 'read'):
        upcoming_insurance = Insurance.objects.filter(
            end_date__lte=today + timedelta(days=30),
            end_date__gte=today
        )
        summary['即将到期'] = {
            '保险(30天内)': upcoming_insurance.count(),
            '保险列表': InsuranceSerializer(upcoming_insurance[:5], many=True).data
        }

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

    # 根据 model 参数搜索指定模型，或搜索所有模型
    models_to_search = {
        'decoration': (Decoration, DecorationSerializer),
        'customer_credit': (CustomerCredit, CustomerCreditSerializer),
        'customer_complaint': (CustomerComplaint, CustomerComplaintSerializer),
        'regulation': (Regulation, RegulationSerializer),
        'price_evaluation': (PriceEvaluation, PriceEvaluationSerializer),
        'daily_record': (DailyRecord, DailyRecordSerializer),
        'insurance': (Insurance, InsuranceSerializer),
        'complaint': (Complaint, ComplaintSerializer),
    }

    if model and model in models_to_search:
        # 指定模块但无 read 权限：403（不得借搜索通道越权读取）
        if not _can(request.user, model, 'read'):
            return Response({'detail': f'无权访问模块 {model}'}, status=403)
        model_class, serializer_class = models_to_search[model]
        # 用 Q 对象合并为单次查询，避免多次 SQL
        text_fields = [f.name for f in model_class._meta.get_fields()
                       if hasattr(f, 'name') and f.name != 'id'
                       and getattr(f, 'get_internal_type', lambda: '')() in ('CharField', 'TextField')]
        q_filter = Q()
        for field in text_fields:
            q_filter |= Q(**{f'{field}__icontains': query})
        results[model] = serializer_class(model_class.objects.filter(q_filter).distinct()[:20], many=True).data
    else:
        # 搜索所有有权限的模型
        for name, (model_class, serializer_class) in models_to_search.items():
            if not _can(request.user, name, 'read'):
                continue
            text_fields = [f.name for f in model_class._meta.get_fields()
                           if hasattr(f, 'name') and f.name != 'id'
                           and getattr(f, 'get_internal_type', lambda: '')() in ('CharField', 'TextField')]
            q_filter = Q()
            for field in text_fields:
                q_filter |= Q(**{f'{field}__icontains': query})
            queryset = model_class.objects.filter(q_filter).distinct()[:10]
            if queryset.exists():
                results[name] = serializer_class(queryset, many=True).data

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
    }

    # 扩展词匹配：支持"铜价"、"金价"等变体
    expanded_map = {
        '铜': '金属', '锡': '金属', '铝': '金属', '锌': '金属',
        '铅': '金属', '镍': '金属', '金': '金属', '银': '金属',
    }

    def match_score(q, keywords):
        """计算问题与映射的匹配得分"""
        score = 0
        q_lower = q.lower()
        for kw in keywords:
            if kw in q:
                score += len(kw)  # 关键词越长得分越高
            if kw in q_lower:
                score += 0.5
        return score

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
        if not _can(request.user, model_name, 'read'):
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
            recent = DailyRecord.objects.all()[:20]
            response['数据'] = DailyRecordSerializer(recent, many=True).data
            response['答案'] = f'共{DailyRecord.objects.count()}条工作记录'

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
    else:
        response['答案'] = '请明确搜索内容，支持：保险、客诉、诉讼、账期、记录、装修、核价、金属、制度'

    return Response(response)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def model_list(request):
    """获取所有可用模型列表"""
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
            'count': DailyRecord.objects.count(),
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
    }
    # 仅返回当前用户有 read 权限的模块
    return Response({k: v for k, v in models.items() if _can(request.user, k, 'read')})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def ai_create(request):
    """AI 创建数据接口"""
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
    }
    
    if model_type not in models_map:
        return Response({'error': '无效的模型类型'}, status=400)

    # RBAC：AI 通道与页面表单同一把锁，无对应模块 create 权限一律 403
    if not _can(request.user, model_type, 'create'):
        return Response({'detail': f'无权在模块 {model_type} 新增数据'}, status=403)

    model_class, serializer_class = models_map[model_type]
    serializer = serializer_class(data=data.get('data', {}))
    
    if serializer.is_valid():
        serializer.save()
        return Response(serializer.data, status=201)
    return Response(serializer.errors, status=400)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def health_check(request):
    """健康检查接口"""
    return Response({
        'status': 'ok',
        'service': 'AI_daily_work_record',
        'version': '1.0.0',
        'ai_ready': True,
        'timestamp': datetime.now().isoformat()
    })


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