from rest_framework import serializers

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


# 系统维护字段：任何业务序列化器都不得通过 API 直写。
# 改造前 fields = '__all__' 把"模型有哪些字段"直接变成了"API 能写哪些字段"，
# 于是 status / is_deleted 这类应由状态机与软删除流程控制的字段出现旁路（审计 #9）。
READ_ONLY_BY_DEFAULT = {
    "id", "pk", "created_at", "updated_at", "created_by",
    "is_deleted", "deleted_at", "deleted_by",
    "registered_at", "terminated_at", "imported_at",
}


class EnhancedModelSerializer(serializers.ModelSerializer):
    """统一收窄可写字段的基类。

    子类仍可写 fields = '__all__'：这里按模型实际字段集取交集后逐个置为只读，
    新增模型字段时不需要再到每个序列化器里补一遍。
    """

    def get_fields(self):
        fields = super().get_fields()
        for name in READ_ONLY_BY_DEFAULT & fields.keys():
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