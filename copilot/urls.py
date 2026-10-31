from django.urls import path

from . import views, views_ops

app_name = 'copilot'

urlpatterns = [
    path('bootstrap/', views.bootstrap, name='bootstrap'),
    path('ask/', views.ask, name='ask'),
    path('run/<uuid:run_id>/', views.run_detail, name='run_detail'),
    path('run/<uuid:run_id>/stream/', views.run_stream, name='run_stream'),
    path('run/<uuid:run_id>/cancel/', views.cancel, name='cancel'),
    path('conversation/latest/', views.latest_conversation, name='latest_conversation'),
    path('conversation/<int:pk>/', views.history, name='history'),
    path('conversation/<int:pk>/rename/', views.rename_conversation, name='rename_conversation'),
    path('conversation/<int:pk>/delete/', views.delete_conversation, name='delete_conversation'),
    path('conversations/', views.conversations, name='conversations'),
    path('feedback/', views.feedback, name='feedback'),
    path('message/export/', views.export_answer, name='export'),
    path('message/draft/', views.save_draft, name='save_draft'),
    # 页面（与上面的接口同名不同层：这里返回 HTML）
    path('quality/', views_ops.quality, name='quality'),
    path('egress/', views_ops.egress_audit, name='egress_audit'),
    path('c/<int:pk>/', views_ops.conversation_page, name='conversation_page'),
]
