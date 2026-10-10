from django.contrib import admin
from .models import KnowledgeCategory, KnowledgeDocument


@admin.register(KnowledgeCategory)
class KnowledgeCategoryAdmin(admin.ModelAdmin):
    list_display = ['name', 'parent', 'sort_order']
    search_fields = ['name']
    ordering = ['sort_order', 'name']


@admin.register(KnowledgeDocument)
class KnowledgeDocumentAdmin(admin.ModelAdmin):
    list_display = ['title', 'category', 'status', 'created_at', 'updated_at', 'view_count']
    search_fields = ['title', 'content', 'tags']
    list_filter = ['category', 'status']
    ordering = ['-updated_at']
