"""AI API 视图门面（复核报告 ARC-02）。

原 1175 行单文件按域拆为：
- `viewsets.py`：分页、增强基类与 24 个业务 ViewSet；
- `views_ai.py`：AI 友好接口（summary/search/query/create/models）与健康检查等。

本文件只做 re-export，`ai_api/urls.py`、`ai_api/tests_hardening.py` 等外部引用
`ai_api.views.<Name>` 完全不变。
"""

from .viewsets import (  # noqa: F401
    CustomPagination,
    EnhancedModelViewSet,
    ReadOnlyEnhancedViewSet,
    DecorationViewSet,
    CustomerCreditViewSet,
    CustomerComplaintViewSet,
    RegulationViewSet,
    PriceEvaluationViewSet,
    DailyRecordViewSet,
    MetalPriceViewSet,
    InsuranceViewSet,
    ComplaintViewSet,
    PartnerViewSet,
    SampleViewSet,
    TestRecordViewSet,
    BatchRecordViewSet,
    AuditRecordViewSet,
    CabinetViewSet,
    ArchiveCategoryViewSet,
    LedgerViewSet,
    ArchiveViewSet,
    BorrowRecordViewSet,
    LicenseViewSet,
    AnnualReviewViewSet,
    LicenseBorrowRecordViewSet,
    KnowledgeCategoryViewSet,
    KnowledgeDocumentViewSet,
)
from .views_ai import (  # noqa: F401
    ai_create,
    ai_query,
    ai_search,
    ai_summary,
    get_background_images,
    health_check,
    model_list,
)

__all__ = [
    "CustomPagination",
    "EnhancedModelViewSet",
    "ReadOnlyEnhancedViewSet",
    "DecorationViewSet",
    "CustomerCreditViewSet",
    "CustomerComplaintViewSet",
    "RegulationViewSet",
    "PriceEvaluationViewSet",
    "DailyRecordViewSet",
    "MetalPriceViewSet",
    "InsuranceViewSet",
    "ComplaintViewSet",
    "PartnerViewSet",
    "SampleViewSet",
    "TestRecordViewSet",
    "BatchRecordViewSet",
    "AuditRecordViewSet",
    "CabinetViewSet",
    "ArchiveCategoryViewSet",
    "LedgerViewSet",
    "ArchiveViewSet",
    "BorrowRecordViewSet",
    "LicenseViewSet",
    "AnnualReviewViewSet",
    "LicenseBorrowRecordViewSet",
    "KnowledgeCategoryViewSet",
    "KnowledgeDocumentViewSet",
    "ai_summary",
    "ai_search",
    "ai_query",
    "model_list",
    "ai_create",
    "health_check",
    "get_background_images",
]
