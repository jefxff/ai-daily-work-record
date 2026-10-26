from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views
from . import api_admin, api_business

# 路由注册
router = DefaultRouter()
router.register(r'decoration', views.DecorationViewSet)
router.register(r'customer-credit', views.CustomerCreditViewSet)
router.register(r'customer-complaint', views.CustomerComplaintViewSet)
router.register(r'regulation', views.RegulationViewSet)
router.register(r'price-evaluation', views.PriceEvaluationViewSet)
router.register(r'daily-record', views.DailyRecordViewSet)
router.register(r'metal-price', views.MetalPriceViewSet)
# 子路径必须先于父级注册：DRF detail 路由的正则是 [^/.]+，先注册 insurance
# 会让 'insurance/subjects/' 被当作 pk='subjects' 的详情路由抢先匹配成 404。
router.register(r'insurance/subjects', views.InsuranceSubjectViewSet, basename='api-insurancesubject')
router.register(r'insurance/policy-years', views.InsurancePolicyYearViewSet, basename='api-insurancepolicyyear')
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
router.register(r'knowledge/folders', views.KnowledgeFolderViewSet, basename='api-knowledgefolder')
# 操作留痕（只读）
router.register(r'operation-logs', views.OperationLogViewSet, basename='api-operationlog')
# 账户管理（用户/角色/登录日志/长期 Token）
router.register(r'accounts/users', api_admin.UserAdminViewSet)
router.register(r'accounts/roles', api_admin.RoleAdminViewSet)
router.register(r'accounts/login-logs', api_admin.LoginLogViewSet, basename='api-loginlog')
router.register(r'accounts/api-tokens', api_admin.ApiTokenViewSet, basename='api-apitoken')
# 通知中心：tasks 必须先于 notifications 注册，否则 'notifications/tasks/' 会被
# notifications 的 detail 路由（pk='tasks'）抢先匹配
router.register(r'notifications/tasks', api_admin.ScheduledTaskViewSet, basename='api-scheduledtask')
router.register(r'notifications', api_admin.NotificationViewSet, basename='api-notification')

urlpatterns = [
    # 静态字面量端点必须放在 router 之前，避免被 detail 路由（pk=字面量）抢先匹配
    path('notifications/preferences/', api_admin.NotificationPreferenceAPIView.as_view(),
         name='api-notification-preferences'),
    path('notifications/announce/', api_admin.AnnounceAPIView.as_view(),
         name='api-notification-announce'),
    path('notifications/task-runs/', api_admin.TaskRunLogAPIView.as_view(),
         name='api-task-run-logs'),
    path('system/health/', api_admin.SystemHealthAPIView.as_view(),
         name='api-system-health'),
    path('system/perf/', api_admin.SystemPerfAPIView.as_view(),
         name='api-system-perf'),

    # 业务聚合 / 报表 / 档案表级导入导出（静态字面量端点，必须早于 router）
    path('daily-record/weekly-report/',
         api_business.WeeklyReportAPIView.as_view(), name='api-weekly-report'),
    path('daily-record/annual-summary/',
         api_business.AnnualSummaryAPIView.as_view(), name='api-annual-summary'),
    path('expiry/calendar/',
         api_business.ExpiryCalendarAPIView.as_view(), name='api-expiry-calendar'),
    path('operation-logs/anomalies/',
         api_business.AuditAnomaliesAPIView.as_view(), name='api-audit-anomalies'),
    path('data-download/summary/',
         api_business.DataDownloadSummaryAPIView.as_view(), name='api-data-download-summary'),
    path('archive/tables/<str:table>/export/',
         api_business.ArchiveTableExportAPIView.as_view(), name='api-archive-table-export'),
    path('archive/tables/<str:table>/template/',
         api_business.ArchiveTableTemplateAPIView.as_view(), name='api-archive-table-template'),
    path('archive/tables/<str:table>/import/',
         api_business.ArchiveTableImportAPIView.as_view(), name='api-archive-table-import'),

    # REST API 路由
    path('', include(router.urls)),

    # 账户管理补充端点
    path('accounts/sessions/', api_admin.SessionAPIView.as_view(), name='api-sessions'),
    path('accounts/sessions/<str:action>/', api_admin.SessionAPIView.as_view(),
         name='api-session-action'),
    
    # AI 友好接口
    path('ai/summary/', views.ai_summary, name='ai_summary'),
    path('ai/search/', views.ai_search, name='ai_search'),
    path('ai/query/', views.ai_query, name='ai_query'),
    path('ai/models/', views.model_list, name='model_list'),
    path('ai/create/', views.ai_create, name='ai_create'),
    path('ai/health/', views.health_check, name='health_check'),
    path('ai/background-images/', views.get_background_images, name='get_background_images'),
]