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
    # 切换可见性（owner 专属，仅 POST）
    path('document/<int:pk>/visibility/', views.document_visibility, name='document_visibility'),
    # 大纲/导图数据源（GET 只读 P1；PUT 整树保存 4b，同路径按方法分发）
    path('api/docs/<int:pk>/tree/', views.api_doc_tree, name='api_doc_tree'),
    # 大纲编辑器页（4b）
    path('document/<int:pk>/outline/', views.outline_editor, name='outline_editor'),
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
    # —— 文件夹树（批次 6 / 方案 04）：GET 树 + POST 新建同路径分发 ——
    path('api/folders/', views.api_folders, name='api_folders'),
    # 项目硬约束：删除类操作一律 POST（方案04的 PATCH/DELETE 改为 POST 子路由）
    path('api/folders/<int:pk>/rename/', views.api_folder_rename, name='api_folder_rename'),
    path('api/folders/<int:pk>/move/', views.api_folder_move, name='api_folder_move'),
    path('api/folders/<int:pk>/delete/', views.api_folder_delete, name='api_folder_delete'),
    # 文档归档/移出（拖拽落点）
    path('api/docs/<int:pk>/move/', views.api_doc_move, name='api_doc_move'),
]
