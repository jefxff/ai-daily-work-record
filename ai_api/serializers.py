from rest_framework import serializers
from records.models import (
    Decoration, CustomerCredit, CustomerComplaint, Regulation,
    PriceEvaluation, DailyRecord, MetalPrice, Insurance, Complaint
)

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