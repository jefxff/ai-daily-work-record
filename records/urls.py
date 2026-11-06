from django.urls import path
from . import views
from . import views_customer_review
from . import views_expiry, views_reports, views_audit, views_workspace, views_insurance

urlpatterns = [
    # 首页
    path('', views.home, name='home'),
    # 待办工作台
    path('workspace/', views_workspace.workspace, name='workspace'),
    # 搜索
    path('search/', views.search, name='search'),
    
    # 装修记录
    path('decoration/', views.decoration_list, name='decoration_list'),
    path('decoration/add/', views.decoration_add, name='decoration_add'),
    path('decoration/detail/<int:pk>/', views.decoration_detail, name='decoration_detail'),
    path('decoration/edit/<int:pk>/', views.decoration_edit, name='decoration_edit'),
    path('decoration/delete/<int:pk>/', views.decoration_delete, name='decoration_delete'),
    path('decoration/export/', views.decoration_export, name='decoration_export'),
    
    # 客户账期
    path('customer_credit/', views.customer_credit_list, name='customer_credit_list'),
    path('customer_credit/add/', views.customer_credit_add, name='customer_credit_add'),
    path('customer_credit/detail/<int:pk>/', views.customer_credit_detail, name='customer_credit_detail'),
    path('customer_credit/launch_review/<int:pk>/', views_customer_review.credit_launch_review, name='customer_credit_launch_review'),
    path('customer_credit/docs/<int:pk>/', views.customer_credit_docs_save, name='customer_credit_docs_save'),
    path('customer_credit/edit/<int:pk>/', views.customer_credit_edit, name='customer_credit_edit'),
    path('customer_credit/delete/<int:pk>/', views.customer_credit_delete, name='customer_credit_delete'),
    path('customer_credit/export/', views.customer_credit_export, name='customer_credit_export'),
    

    # 客户评审（信用评价 / 定期考评 / 现场审核）
    path('customer_review/', views_customer_review.customer_review_list, name='customer_review_list'),
    path('customer_review/add/<str:review_type>/', views_customer_review.review_add, name='customer_review_add'),
    path('customer_review/detail/<int:pk>/', views_customer_review.review_detail, name='customer_review_detail'),
    path('customer_review/edit/<int:pk>/', views_customer_review.review_edit, name='customer_review_edit'),
    path('customer_review/print/<int:pk>/', views_customer_review.review_print, name='customer_review_print'),
    path('customer_review/delete/<int:pk>/', views_customer_review.customer_review_delete, name='customer_review_delete'),
    path('customer_review/ratio_preview/', views_customer_review.ratio_preview, name='customer_review_ratio_preview'),

    # 客户评审：Excel 导出 / 模板 / 导入（勾稽在 customer_review_io 内）
    path('customer_review/export/', views_customer_review.review_export_list, name='customer_review_export'),
    path('customer_review/export/<int:pk>/', views_customer_review.review_export_one, name='customer_review_export_one'),
    path('customer_review/template/', views_customer_review.review_template, name='customer_review_template'),
    path('customer_review/import/', views_customer_review.review_import, name='customer_review_import'),
    path('customer_review/import/confirm/', views_customer_review.review_import_confirm, name='customer_review_import_confirm'),

    # 付款穿行测试（长前缀必须排在 /customer_review/ 之前，面包屑按前缀匹配）
    path('customer_review/walkthrough/', views_customer_review.walkthrough_list, name='walkthrough_list'),
    path('customer_review/walkthrough/add/', views_customer_review.walkthrough_add, name='walkthrough_add'),
    path('customer_review/walkthrough/detail/<int:pk>/', views_customer_review.walkthrough_detail, name='walkthrough_detail'),
    path('customer_review/walkthrough/edit/<int:pk>/', views_customer_review.walkthrough_edit, name='walkthrough_edit'),
    path('customer_review/walkthrough/print/<int:pk>/', views_customer_review.walkthrough_print, name='walkthrough_print'),
    path('customer_review/walkthrough/delete/<int:pk>/', views_customer_review.walkthrough_delete, name='walkthrough_delete'),
    path('customer_review/walkthrough/export/', views_customer_review.walkthrough_export_list, name='walkthrough_export'),
    path('customer_review/walkthrough/export/<int:pk>/', views_customer_review.walkthrough_export_one, name='walkthrough_export_one'),
    path('customer_review/walkthrough/template/', views_customer_review.walkthrough_template, name='walkthrough_template'),
    path('customer_review/walkthrough/import/', views_customer_review.walkthrough_import, name='walkthrough_import'),
    path('customer_review/walkthrough/import/confirm/', views_customer_review.walkthrough_import_confirm, name='walkthrough_import_confirm'),

    # 客户客诉
    path('customer_complaint/', views.customer_complaint_list, name='customer_complaint_list'),
    path('customer_complaint/add/', views.customer_complaint_add, name='customer_complaint_add'),
    path('customer_complaint/edit/<int:pk>/', views.customer_complaint_edit, name='customer_complaint_edit'),
    path('customer_complaint/delete/<int:pk>/', views.customer_complaint_delete, name='customer_complaint_delete'),
    path('customer_complaint/export/', views.customer_complaint_export, name='customer_complaint_export'),
    path('customer_complaint/close/<int:pk>/', views.customer_complaint_close, name='customer_complaint_close'),
    path('customer_detail/<int:pk>/', views.customer_detail, name='customer_detail'),
    
    # 客户台账
    path('customer_ledger/', views.customer_ledger_list, name='customer_ledger_list'),
    path('customer_ledger/add/', views.customer_ledger_add, name='customer_ledger_add'),
    path('customer_ledger/detail/<int:pk>/', views.customer_ledger_detail, name='customer_ledger_detail'),
    path('customer_ledger/edit/<int:pk>/', views.customer_ledger_edit, name='customer_ledger_edit'),
    path('customer_ledger/delete/<int:pk>/', views.customer_ledger_delete, name='customer_ledger_delete'),
    path('customer_ledger/export/', views.customer_ledger_export, name='customer_ledger_export'),
    path('customer_ledger/import/', views.customer_ledger_import, name='customer_ledger_import'),
    path('customer_ledger/import/confirm/', views.customer_ledger_import_confirm, name='customer_ledger_import_confirm'),
    path('customer_ledger/template/', views.customer_ledger_template, name='customer_ledger_template'),
    path('customer_ledger/<int:pk>/attachment/', views.customer_ledger_attachment, name='customer_ledger_attachment'),
    
    # 制度及处罚
    path('regulation/', views.regulation_list, name='regulation_list'),
    path('regulation/add/', views.regulation_add, name='regulation_add'),
    path('regulation/edit/<int:pk>/', views.regulation_edit, name='regulation_edit'),
    path('regulation/delete/<int:pk>/', views.regulation_delete, name='regulation_delete'),
    path('regulation/export/', views.regulation_export, name='regulation_export'),
    path('regulation_detail/<int:pk>/', views.regulation_detail, name='regulation_detail'),
    
    # 核价
    path('price_evaluation/', views.price_evaluation_list, name='price_evaluation_list'),
    path('price_evaluation/add/', views.price_evaluation_add, name='price_evaluation_add'),
    path('price_evaluation/detail/<int:pk>/', views.price_evaluation_detail, name='price_evaluation_detail'),
    path('price_evaluation/edit/<int:pk>/', views.price_evaluation_edit, name='price_evaluation_edit'),
    path('price_evaluation/delete/<int:pk>/', views.price_evaluation_delete, name='price_evaluation_delete'),
    path('price_evaluation/export/', views.price_evaluation_export, name='price_evaluation_export'),
    
    # 每日记录
    path('daily_record/', views.daily_record_list, name='daily_record_list'),
    path('daily_record/incomplete/', views.daily_record_incomplete, name='daily_record_incomplete'),
    path('daily_record/add/', views.daily_record_add, name='daily_record_add'),
    path('daily_record/detail/<int:pk>/', views.daily_record_detail, name='daily_record_detail'),
    path('daily_record/edit/<int:pk>/', views.daily_record_edit, name='daily_record_edit'),
    path('daily_record/delete/<int:pk>/', views.daily_record_delete, name='daily_record_delete'),
    path('daily_record/export/', views.daily_record_export, name='daily_record_export'),
    # 列表页批量操作：标记完成 / 删除（仅 POST，权限按动作分别校验）
    path('daily_record/batch/', views.daily_record_batch, name='daily_record_batch'),
    # 批量沉淀到知识库（仅 POST；一律私有，D5）
    path('daily_record/kb/sync-batch/', views.daily_record_kb_sync_batch, name='daily_record_kb_sync_batch'),
    
    # 保险
    path('insurance/', views.insurance_list, name='insurance_list'),
    path('insurance/add/', views.insurance_add, name='insurance_add'),
    path('insurance/detail/<int:pk>/', views.insurance_detail, name='insurance_detail'),
    path('insurance/edit/<int:pk>/', views.insurance_edit, name='insurance_edit'),
    path('insurance/delete/<int:pk>/', views.insurance_delete, name='insurance_delete'),
    path('insurance/export/', views.insurance_export, name='insurance_export'),
    # 保险只读聚合（阶段 1）与续保录入（阶段 2）
    path('insurance/subjects/', views_insurance.insurance_subject_list, name='insurance_subject_list'),
    path('insurance/subjects/detail/', views_insurance.insurance_subject_detail, name='insurance_subject_detail'),
    path('insurance/renewal_report/', views_insurance.insurance_renewal_report, name='insurance_renewal_report'),
    path('insurance/renew/', views_insurance.insurance_renew, name='insurance_renew'),
    path('insurance/subjects/one-off/', views_insurance.insurance_subject_one_off, name='insurance_subject_one_off'),
    
    # 金属价格详情
    path('metal_price/<str:metal_name>/', views.metal_price_detail, name='metal_price_detail'),
    
    # 刷新金属价格
    path('refresh_metal_prices/', views.refresh_metal_prices, name='refresh_metal_prices'),
    
    # 客诉
    path('complaint/', views.complaint_list, name='complaint_list'),
    path('complaint/add/', views.complaint_add, name='complaint_add'),
    path('complaint/detail/<int:pk>/', views.complaint_detail, name='complaint_detail'),
    path('complaint/edit/<int:pk>/', views.complaint_edit, name='complaint_edit'),
    path('complaint/delete/<int:pk>/', views.complaint_delete, name='complaint_delete'),
    path('complaint/export/', views.complaint_export, name='complaint_export'),
    path('complaint/import/', views.complaint_import, name='complaint_import'),
    path('complaint/import/confirm/', views.complaint_import_confirm, name='complaint_import_confirm'),
    path('complaint/template/', views.complaint_template, name='complaint_template'),
    path('complaint/advance/<int:pk>/', views.complaint_advance, name='complaint_advance'),

    # 数据展示看板
    path('dashboard/', views.dashboard, name='dashboard'),

    # 数据下载页面
    path('data_download/', views.data_download, name='data_download'),

    # 操作留痕
    path('operation_log/', views.operation_log_list, name='operation_log_list'),

    # 到期预警（日历 + 本月到期清单导出）
    path('expiry/calendar/', views_expiry.expiry_calendar, name='expiry_calendar'),
    path('expiry/export/', views_expiry.expiry_export, name='expiry_export'),

    # 每日记录：周报生成 / 年度个人总结
    path('daily_record/weekly_report/', views_reports.weekly_report, name='weekly_report'),
    path('daily_record/annual_summary/', views_reports.annual_summary, name='annual_summary'),

    # 审计异常行为检测（读取层）
    path('operation_log/anomalies/', views_audit.audit_anomalies, name='audit_anomalies'),
]