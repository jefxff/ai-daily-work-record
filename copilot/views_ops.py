"""AI 助手运维页：质量成本看板 + 外发审计 + 会话直达页。

与 ``views.py``（接口）分开的理由：这一组是**页面**，请求不走 JSON 约定，
也不参与面板的调用链；混在一起只会让"哪个视图返回 JSON"这件事变得模糊。

三个页面的可见性刻意不同：
- 看板 ``system.read``：目标是让大家都能看 AI 花了多少、答得怎么样；
- 外发审计 ``system.update``：能看到"谁把什么发给了哪家模型"属于合规敏感
  信息，按运维管理员口径收紧；
- 会话页：本人的会话（``copilot.read``），通知链接点进来直接追问。
"""
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from accounts.decorators import perm_required

from . import metrics
from .models import CopilotConversation, CopilotEgressLog

DAY_CHOICES = (7, 30, 90)
PAGE_SIZE = 30


def _days(request):
    try:
        value = int(request.GET.get('days') or 30)
    except (TypeError, ValueError):
        return 30
    return value if value in DAY_CHOICES else 30


@perm_required('system', 'read')
def quality(request):
    """AI 质量与成本看板（只读）。

    按天筛选只给 7/30/90 三档：自由填起止日期是给自己挖坑（时区、
    跨夏令时、SQL 范围），而实际用得到的就这三种。
    """
    days = _days(request)
    data = metrics.overview(days=days)
    top = data['top_modules']
    return render(request, 'copilot/quality.html', {
        'data': data,
        'days': days,
        'day_choices': DAY_CHOICES,
        'top_modules': top,
        'max_module_calls': max([m['calls'] for m in top] or [1]),
        'now': timezone.now(),
    })


@perm_required('system', 'update')
def egress_audit(request):
    """外发审计查询（合规用途）：谁、什么时候、把哪些模块发给了哪家模型。"""
    rows = CopilotEgressLog.objects.select_related('user').order_by('-created_at')

    username = (request.GET.get('user') or '').strip()
    module = (request.GET.get('module') or '').strip()
    provider = (request.GET.get('provider') or '').strip()
    local_only = request.GET.get('local') == '1'

    if username:
        rows = rows.filter(user__username__icontains=username)
    if module:
        rows = rows.filter(module_keys__icontains=module)
    if provider:
        rows = rows.filter(provider__icontains=provider)
    if local_only:
        rows = rows.filter(is_local=True)

    paginator = Paginator(rows, PAGE_SIZE)
    page = paginator.get_page(request.GET.get('page'))
    return render(request, 'copilot/egress_audit.html', {
        'page': page,
        'filters': {
            'user': username, 'module': module,
            'provider': provider, 'local': local_only,
        },
        'total': paginator.count,
    })


@perm_required('copilot', 'read')
def conversation_page(request, pk):
    """会话直达页（通知里的"点开继续追问"落到这里）。

    面板本身读的是 localStorage  + ``/conversation/latest/``，URL 里带
    会话 id 它认不出来；这里把 id 交给页面，由脚本通知面板切换过去。
    """
    conversation = get_object_or_404(CopilotConversation, pk=pk, user=request.user)
    return render(request, 'copilot/conversation.html', {
        'conversation': conversation,
    })
