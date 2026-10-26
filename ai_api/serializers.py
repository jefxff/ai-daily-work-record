from rest_framework import serializers

from records.models import (
    Decoration, CustomerCredit, CustomerComplaint, Regulation,
    PriceEvaluation, DailyRecord, MetalPrice, Insurance, Complaint,
    InsuranceSubject, InsurancePolicyYear, OperationLog,
)
from samples.models import Partner, Sample, TestRecord, BatchRecord, AuditRecord
from archive.models import (
    Cabinet, Category as ArchiveCategory, Ledger, Archive,
    BorrowRecord, License, AnnualReview, LicenseBorrowRecord,
)
from knowledge_base.models import KnowledgeCategory, KnowledgeDocument, KnowledgeFolder


# 系统维护字段：任何业务序列化器都不得通过 API 直写。
# 改造前 fields = '__all__' 把"模型有哪些字段"直接变成了"API 能写哪些字段"，
# 于是 status / is_deleted 这类应由状态机与软删除流程控制的字段出现旁路（审计 #9）。
READ_ONLY_BY_DEFAULT = {
    "id", "pk", "created_at", "updated_at", "created_by",
    "is_deleted", "deleted_at", "deleted_by",
    "registered_at", "terminated_at", "imported_at",
}

# 身份类字段（审计 BE-01/FE-09）：经办人/审核人由服务端按当前登录用户注入，
# 页面端走 _current_name() 唯一事实源；API 若允许自由填写即可伪造"谁经手的"，
# 与页面口径不一致。对应视图集在 perform_create/perform_update 里 save(kwargs) 注入。
IDENTITY_READ_ONLY_FIELDS = {"operator", "handler", "reviewer"}


class EnhancedModelSerializer(serializers.ModelSerializer):
    """统一收窄可写字段的基类。

    子类仍可写 fields = '__all__'：这里按模型实际字段集取交集后逐个置为只读，
    新增模型字段时不需要再到每个序列化器里补一遍。
    """

    def get_fields(self):
        fields = super().get_fields()
        for name in READ_ONLY_BY_DEFAULT & fields.keys():
            fields[name].read_only = True
        # 身份类字段一律只读（写值由视图集注入，见 IDENTITY_READ_ONLY_FIELDS 注释）
        for name in IDENTITY_READ_ONLY_FIELDS & fields.keys():
            fields[name].read_only = True
        return fields
# 装修记录序列化
class DecorationSerializer(EnhancedModelSerializer):
    class Meta:
        model = Decoration
        fields = '__all__'

# 客户账期序列化
class CustomerCreditSerializer(EnhancedModelSerializer):
    class Meta:
        model = CustomerCredit
        fields = '__all__'

# 客户客诉序列化
class CustomerComplaintSerializer(EnhancedModelSerializer):
    """客诉 API 序列化器。

    complaint_status 禁止直接改为「已完成」——必须通过闭环动作推导。
    校验与 models.py clean() 双重复盖，形成表单 + API 双重防线。
    """

    def validate(self, attrs):
        status = attrs.get('complaint_status')
        # 只有当请求要把状态改成「已完成」时才拦截
        if status == '已完成':
            existing = getattr(self.instance, 'complaint_status', None)
            closed_at = getattr(self.instance, 'closed_at', None) if self.instance else None
            if existing != '已完成' and not closed_at:
                raise serializers.ValidationError(
                    {'complaint_status': '客诉状态不能直接改为「已完成」，请通过闭环接口操作'}
                )
        return attrs

    class Meta:
        model = CustomerComplaint
        fields = '__all__'

# 制度及处罚序列化
class RegulationSerializer(EnhancedModelSerializer):
    class Meta:
        model = Regulation
        fields = '__all__'

# 核价序列化
class PriceEvaluationSerializer(EnhancedModelSerializer):
    reduction_amount = serializers.ReadOnlyField()

    class Meta:
        model = PriceEvaluation
        fields = '__all__'

# 每日记录序列化
class DailyRecordSerializer(EnhancedModelSerializer):
    # 归属用户只读：归属由服务端在创建时强制绑定，禁止通过 API 指定
    user = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = DailyRecord
        fields = '__all__'

# 金属价格序列化
class MetalPriceSerializer(EnhancedModelSerializer):
    is_expired = serializers.ReadOnlyField()
    expired_days = serializers.ReadOnlyField()

    class Meta:
        model = MetalPrice
        fields = '__all__'
        extra_kwargs = {'price': {'required': False, 'allow_null': True}}

# 保险序列化
class InsuranceSerializer(EnhancedModelSerializer):
    is_expired = serializers.ReadOnlyField()
    expired_days = serializers.ReadOnlyField()
    # 只读字段：供外部按年度/保司/当前有效期筛选，不影响写操作
    policy_year_year = serializers.IntegerField(source='policy_year.year', read_only=True, default=None)
    subject_label = serializers.CharField(source='subject.label', read_only=True, default='')
    insurer_canonical = serializers.SerializerMethodField()
    is_current_term = serializers.SerializerMethodField()

    def get_insurer_canonical(self, obj):
        from records.insurance_grouping import normalize_insurer
        return normalize_insurer(obj.company_name)

    def get_is_current_term(self, obj):
        """判断该保单是否为其标的的最新一期（当前保障是否有效）。"""
        if not obj.subject_id:
            return None
        # 标的最新一期的截止日期就是「当前是否在保」的判定基准
        latest_end = obj.subject.records.order_by('-end_date').values_list('end_date', flat=True).first()
        return obj.end_date == latest_end

    class Meta:
        model = Insurance
        fields = '__all__'

# 客诉序列化
class ComplaintSerializer(EnhancedModelSerializer):
    class Meta:
        model = Complaint
        fields = '__all__'


# ==================== 试样管理 ====================

# 供应商档案序列化
class PartnerSerializer(EnhancedModelSerializer):
    class Meta:
        model = Partner
        fields = '__all__'


# 样品主档序列化
class SampleSerializer(EnhancedModelSerializer):
    class Meta:
        model = Sample
        fields = '__all__'
        # 状态只能由 samples.services 的状态机推进，API 不可直写
        read_only_fields = ("status", "sample_no")


# 试样记录序列化
class TestRecordSerializer(EnhancedModelSerializer):
    class Meta:
        model = TestRecord
        fields = '__all__'


# 小批量记录序列化
class BatchRecordSerializer(EnhancedModelSerializer):
    class Meta:
        model = BatchRecord
        fields = '__all__'


# 审厂记录序列化
class AuditRecordSerializer(EnhancedModelSerializer):
    class Meta:
        model = AuditRecord
        fields = '__all__'


# ==================== 档案管理 ====================

# 档案柜序列化
class CabinetSerializer(EnhancedModelSerializer):
    class Meta:
        model = Cabinet
        fields = '__all__'


# 档案类别序列化
class ArchiveCategorySerializer(EnhancedModelSerializer):
    full_path = serializers.ReadOnlyField()

    class Meta:
        model = ArchiveCategory
        fields = '__all__'


# 台账序列化
class LedgerSerializer(EnhancedModelSerializer):
    archive_count = serializers.ReadOnlyField()

    class Meta:
        model = Ledger
        fields = '__all__'


# 档案序列化
class ArchiveSerializer(EnhancedModelSerializer):
    is_borrowed = serializers.ReadOnlyField()
    is_expiring = serializers.ReadOnlyField()
    location_path = serializers.ReadOnlyField()
    tags_list = serializers.ReadOnlyField()
    # 创建人由服务端维护，AI 通道不可指定
    creator = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = Archive
        fields = '__all__'


# 档案借阅记录序列化
class BorrowRecordSerializer(EnhancedModelSerializer):
    is_overdue = serializers.ReadOnlyField()

    class Meta:
        model = BorrowRecord
        fields = '__all__'


# 证照序列化
class LicenseSerializer(EnhancedModelSerializer):
    status = serializers.ReadOnlyField()
    status_display = serializers.ReadOnlyField()
    is_expiring = serializers.ReadOnlyField()
    is_expired = serializers.ReadOnlyField()
    is_borrowed = serializers.ReadOnlyField()
    # 创建人由服务端维护，AI 通道不可指定
    creator = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = License
        fields = '__all__'


# 年审记录序列化
class AnnualReviewSerializer(EnhancedModelSerializer):
    class Meta:
        model = AnnualReview
        fields = '__all__'


# 证照借阅记录序列化
class LicenseBorrowRecordSerializer(EnhancedModelSerializer):
    is_overdue = serializers.ReadOnlyField()

    class Meta:
        model = LicenseBorrowRecord
        fields = '__all__'


# ==================== 知识库 ====================

# 知识分类序列化
class KnowledgeCategorySerializer(EnhancedModelSerializer):
    class Meta:
        model = KnowledgeCategory
        fields = '__all__'


# 知识文档序列化
class KnowledgeDocumentSerializer(EnhancedModelSerializer):
    tag_list = serializers.ReadOnlyField()

    class Meta:
        model = KnowledgeDocument
        fields = '__all__'


# ==================== 保险标的 / 保单年度（只读聚合） ====================

class InsuranceSubjectSerializer(EnhancedModelSerializer):
    kind_label = serializers.CharField(source='get_kind_display', read_only=True)

    class Meta:
        model = InsuranceSubject
        fields = [
            'id', 'kind', 'kind_label', 'label', 'plate', 'insurance_type',
            'department', 'remarks', 'is_one_off', 'created_at', 'updated_at',
        ]


class InsurancePolicyYearSerializer(EnhancedModelSerializer):
    subject_label = serializers.CharField(source='subject.label', read_only=True)
    subject_plate = serializers.CharField(source='subject.plate', read_only=True)
    kind = serializers.CharField(source='subject.kind', read_only=True)

    class Meta:
        model = InsurancePolicyYear
        fields = [
            'id', 'subject', 'subject_label', 'subject_plate', 'kind',
            'year', 'start_date', 'end_date', 'premium', 'insurer',
            'created_at', 'updated_at',
        ]


# ==================== 操作留痕（只读） ====================

class OperationLogSerializer(EnhancedModelSerializer):
    change_diff = serializers.ReadOnlyField()

    class Meta:
        model = OperationLog
        fields = [
            'id', 'operation_time', 'operator', 'operator_username',
            'module_type', 'operation_type', 'object_identifier',
            'change_summary', 'before_json', 'after_json', 'change_diff',
        ]


# ==================== 知识库文件夹（个人书架） ====================

class KnowledgeFolderSerializer(EnhancedModelSerializer):
    parent_name = serializers.CharField(source='parent.name', read_only=True, default='')

    class Meta:
        model = KnowledgeFolder
        fields = ['id', 'name', 'parent', 'parent_name', 'sort', 'created_at']
        read_only_fields = ['id', 'created_at']