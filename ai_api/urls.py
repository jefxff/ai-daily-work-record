from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views

# 路由注册
router = DefaultRouter()
router.register(r'decoration', views.DecorationViewSet)
router.register(r'customer-credit', views.CustomerCreditViewSet)
router.register(r'customer-complaint', views.CustomerComplaintViewSet)
router.register(r'regulation', views.RegulationViewSet)
router.register(r'price-evaluation', views.PriceEvaluationViewSet)
router.register(r'daily-record', views.DailyRecordViewSet)
router.register(r'metal-price', views.MetalPriceViewSet)
router.register(r'insurance', views.InsuranceViewSet)
router.register(r'complaint', views.ComplaintViewSet)
# 试样管理
router.register(r'samples/partner', views.PartnerViewSet)
router.register(r'samples/sample', views.SampleViewSet)
router.register(r'samples/test-record', views.TestRecordViewSet)
router.register(r'samples/batch-record', views.BatchRecordViewSet)
router.register(r'samples/audit-record', views.AuditRecordViewSet)
# 档案管理
router.register(r'archive/cabinet', views.CabinetViewSet)
router.register(r'archive/category', views.ArchiveCategoryViewSet)
router.register(r'archive/ledger', views.LedgerViewSet)
router.register(r'archive/archive', views.ArchiveViewSet)
router.register(r'archive/borrow', views.BorrowRecordViewSet)
router.register(r'archive/license', views.LicenseViewSet)
router.register(r'archive/annual-review', views.AnnualReviewViewSet)
router.register(r'archive/license-borrow', views.LicenseBorrowRecordViewSet)
# 知识库
router.register(r'knowledge/category', views.KnowledgeCategoryViewSet)
router.register(r'knowledge/document', views.KnowledgeDocumentViewSet)

urlpatterns = [
    # REST API 路由
    path('', include(router.urls)),
    
    # AI 友好接口
    path('ai/summary/', views.ai_summary, name='ai_summary'),
    path('ai/search/', views.ai_search, name='ai_search'),
    path('ai/query/', views.ai_query, name='ai_query'),
    path('ai/models/', views.model_list, name='model_list'),
    path('ai/create/', views.ai_create, name='ai_create'),
    path('ai/health/', views.health_check, name='health_check'),
    path('ai/background-images/', views.get_background_images, name='get_background_images'),
]