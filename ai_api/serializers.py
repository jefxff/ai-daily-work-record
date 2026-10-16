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

# 装修记录序列化
class DecorationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Decoration
        fields = '__all__'

# 客户账期序列化
class CustomerCreditSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomerCredit
        fields = '__all__'

# 客户客诉序列化
class CustomerComplaintSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomerComplaint
        fields = '__all__'

# 制度及处罚序列化
class RegulationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Regulation
        fields = '__all__'

# 核价序列化
class PriceEvaluationSerializer(serializers.ModelSerializer):
    reduction_amount = serializers.ReadOnlyField()

    class Meta:
        model = PriceEvaluation
        fields = '__all__'

# 每日记录序列化
class DailyRecordSerializer(serializers.ModelSerializer):
    # 归属用户只读：归属由服务端在创建时强制绑定，禁止通过 API 指定
    user = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = DailyRecord
        fields = '__all__'

# 金属价格序列化
class MetalPriceSerializer(serializers.ModelSerializer):
    is_expired = serializers.ReadOnlyField()
    expired_days = serializers.ReadOnlyField()

    class Meta:
        model = MetalPrice
        fields = '__all__'
        extra_kwargs = {'price': {'required': False, 'allow_null': True}}

# 保险序列化
class InsuranceSerializer(serializers.ModelSerializer):
    is_expired = serializers.ReadOnlyField()
    expired_days = serializers.ReadOnlyField()

    class Meta:
        model = Insurance
        fields = '__all__'

# 客诉序列化
class ComplaintSerializer(serializers.ModelSerializer):
    class Meta:
        model = Complaint
        fields = '__all__'


# ==================== 试样管理 ====================

# 供应商档案序列化
class PartnerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Partner
        fields = '__all__'


# 样品主档序列化
class SampleSerializer(serializers.ModelSerializer):
    class Meta:
        model = Sample
        fields = '__all__'


# 试样记录序列化
class TestRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = TestRecord
        fields = '__all__'


# 小批量记录序列化
class BatchRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = BatchRecord
        fields = '__all__'


# 审厂记录序列化
class AuditRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditRecord
        fields = '__all__'


# ==================== 档案管理 ====================

# 档案柜序列化
class CabinetSerializer(serializers.ModelSerializer):
    class Meta:
        model = Cabinet
        fields = '__all__'


# 档案类别序列化
class ArchiveCategorySerializer(serializers.ModelSerializer):
    full_path = serializers.ReadOnlyField()

    class Meta:
        model = ArchiveCategory
        fields = '__all__'


# 台账序列化
class LedgerSerializer(serializers.ModelSerializer):
    archive_count = serializers.ReadOnlyField()

    class Meta:
        model = Ledger
        fields = '__all__'


# 档案序列化
class ArchiveSerializer(serializers.ModelSerializer):
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
class BorrowRecordSerializer(serializers.ModelSerializer):
    is_overdue = serializers.ReadOnlyField()

    class Meta:
        model = BorrowRecord
        fields = '__all__'


# 证照序列化
class LicenseSerializer(serializers.ModelSerializer):
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
class AnnualReviewSerializer(serializers.ModelSerializer):
    class Meta:
        model = AnnualReview
        fields = '__all__'


# 证照借阅记录序列化
class LicenseBorrowRecordSerializer(serializers.ModelSerializer):
    is_overdue = serializers.ReadOnlyField()

    class Meta:
        model = LicenseBorrowRecord
        fields = '__all__'


# ==================== 知识库 ====================

# 知识分类序列化
class KnowledgeCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = KnowledgeCategory
        fields = '__all__'


# 知识文档序列化
class KnowledgeDocumentSerializer(serializers.ModelSerializer):
    tag_list = serializers.ReadOnlyField()

    class Meta:
        model = KnowledgeDocument
        fields = '__all__'