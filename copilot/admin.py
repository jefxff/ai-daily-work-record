from django.contrib import admin

from .models import (CopilotConversation, CopilotEgressLog, CopilotEvalCase,
                     CopilotFeedback, CopilotMessage, CopilotPolicy, CopilotRun,
                     CopilotToolCall)


@admin.register(CopilotPolicy)
class CopilotPolicyAdmin(admin.ModelAdmin):
    list_display = ('id', 'mask_enabled', 'rate_limit_per_10min',
                    'rate_limit_per_day', 'max_tool_rounds', 'updated_at')
    readonly_fields = ('updated_at',)


class ReadOnlyAdmin(admin.ModelAdmin):
    """审计类数据：只允许查看，不允许在后台改（改了就失去审计意义）。"""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return bool(request.user.is_superuser)


@admin.register(CopilotConversation)
class CopilotConversationAdmin(ReadOnlyAdmin):
    list_display = ('id', 'user', 'title', 'status', 'token_total', 'last_active_at')
    list_filter = ('status',)
    search_fields = ('title', 'user__username')


@admin.register(CopilotMessage)
class CopilotMessageAdmin(ReadOnlyAdmin):
    list_display = ('id', 'conversation', 'role', 'short_content', 'provider',
                    'model', 'error_code', 'created_at')
    list_filter = ('role', 'error_code')
    search_fields = ('content',)

    @admin.display(description='内容')
    def short_content(self, obj):
        return (obj.content or '')[:50]


@admin.register(CopilotRun)
class CopilotRunAdmin(ReadOnlyAdmin):
    list_display = ('id', 'user', 'status', 'provider', 'model', 'total_tokens',
                    'created_at', 'finished_at')
    list_filter = ('status', 'provider', 'model')
    search_fields = ('question',)


@admin.register(CopilotToolCall)
class CopilotToolCallAdmin(ReadOnlyAdmin):
    list_display = ('id', 'tool_name', 'permitted', 'denied_reason', 'row_count',
                    'latency_ms', 'created_at')
    list_filter = ('tool_name', 'permitted')


@admin.register(CopilotEgressLog)
class CopilotEgressLogAdmin(ReadOnlyAdmin):
    list_display = ('id', 'user', 'provider', 'model', 'is_local', 'row_count',
                    'created_at')
    list_filter = ('provider', 'is_local')


@admin.register(CopilotEvalCase)
class CopilotEvalCaseAdmin(admin.ModelAdmin):
    list_display = ('id', 'question', 'module_scope', 'active', 'created_at')
    list_filter = ('active', 'module_scope')
    search_fields = ('question',)


@admin.register(CopilotFeedback)
class CopilotFeedbackAdmin(ReadOnlyAdmin):
    list_display = ('id', 'message', 'user', 'vote', 'created_at')
    list_filter = ('vote',)
