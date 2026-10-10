from django.urls import path
from . import views

app_name = 'knowledge_base'

urlpatterns = [
    # 文档列表
    path('', views.document_list, name='document_list'),
    # 文档详情
    path('document/<int:pk>/', views.document_detail, name='document_detail'),
    # 创建文档
    path('document/create/', views.document_create, name='document_create'),
    # 编辑文档
    path('document/<int:pk>/edit/', views.document_edit, name='document_edit'),
    # 删除文档
    path('document/<int:pk>/delete/', views.document_delete, name='document_delete'),
    # 分类列表
    path('categories/', views.category_list, name='category_list'),
    # 创建分类
    path('category/create/', views.category_create, name='category_create'),
    # 编辑分类
    path('category/<int:pk>/edit/', views.category_edit, name='category_edit'),
    # 删除分类
    path('category/<int:pk>/delete/', views.category_delete, name='category_delete'),
    # 搜索
    path('search/', views.document_search, name='document_search'),
]
