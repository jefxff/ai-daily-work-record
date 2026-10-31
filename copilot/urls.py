from django.urls import path

from . import views

app_name = 'copilot'

urlpatterns = [
    path('bootstrap/', views.bootstrap, name='bootstrap'),
    path('ask/', views.ask, name='ask'),
    path('run/<uuid:run_id>/', views.run_detail, name='run_detail'),
    path('run/<uuid:run_id>/cancel/', views.cancel, name='cancel'),
    path('conversation/latest/', views.latest_conversation, name='latest_conversation'),
    path('conversation/<int:pk>/', views.history, name='history'),
    path('feedback/', views.feedback, name='feedback'),
]
