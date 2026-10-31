"""Copilot 接口：bootstrap / ask / run 轮询 / cancel / history / feedback。

约定（与 docs/api-conventions.md 一致）：
- 成功 ``{"ok": true, "data": {...}}``；失败 ``{"ok": false, "error": CODE, "message": "..."}``；
- 所有视图都挂 ``@perm_required('copilot', 'read')``，数据按 ``request.user`` 隔离；
- 前端 fetch 带 ``X-Requested-With: XMLHttpRequest``，权限失败返回 JSON 403 而不是跳转页面。
"""

import json
import logging
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from accounts.decorators import perm_required

from . import markdown_render, registry, security, tasks
from .engine import prompt as prompt_engine
from .models import (CopilotConversation, CopilotFeedback, CopilotMessage,
                     CopilotPolicy, CopilotRun)
from .providers import router

logger = logging.getLogger(__name__)

MAX_QUESTION = 500
REAP_THROTTLE_SECONDS = 10


def _run_timeout():
    return getattr(settings, 'COPILOT_RUN_TIMEOUT', 120)


def _payload(data, status=200):
    return JsonResponse({'ok': True, 'data': data}, status=status)


def _error(code, message, status=400, **extra):
    body = {'ok': False, 'error': code, 'message': message}
    body.update(extra)
    return JsonResponse(body, status=status)


def _body(request):
    try:
        value = json.loads(request.body or b'{}')
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _run_data(run):
    return {
        'run_id': str(run.pk),
        'conversation_id': run.conversation_id,
        'status': run.status,
        'question': run.question,
        'answer': run.answer,
        # 正文的 HTML 版本：服务端渲染 + 白名单净化，前端只 innerHTML。
        # 直接让浏览器渲染模型原文 = 把 XSS 口子开给"不可信的文本"。
        'answer_html': markdown_render.render(run.answer),
        'refs': run.refs,
        'steps': run.steps,
        'provider': run.provider,
        'model': run.model,
        'error_code': run.error_code,
        'finished': run.is_terminal,
    }


def _maybe_reap():
    """轮询时顺带回收超时任务，最多每 10 秒扫一次（避免每次轮询全表扫）。"""
    try:
        if cache.add('copilot:reap:lock', 1, REAP_THROTTLE_SECONDS):
            tasks.reap_stale_runs()
    except Exception:  # noqa: BLE001 - 缓存不可用就不回收，不影响主流程
        pass


@require_GET
@perm_required('copilot', 'read')
def bootstrap(request):
    policy = CopilotPolicy.current()
    candidates = router.candidates('chat')
    configured = bool(candidates)
    is_local = candidates[0].is_local if candidates else True
    # 借用=「对话问答」还没自己的配置，实际用的是别的用途（通常是润色）那行。
    # 不告诉用户的话，会出现"我去配置页改成本地模型了，徽章还是云端"的困惑。
    borrowed = bool(candidates) and candidates[0].purpose != 'chat'
    modules = registry.available_modules(request.user, is_local, policy)
    blocked = []
    if not is_local:
        visible = {m.key for m in modules}
        blocked = [m.label for m in registry.available_modules(request.user, True, policy)
                   if m.key not in visible]
    return _payload({
        'modules': [{'key': m.key, 'label': m.label, 'level': m.level} for m in modules],
        'blocked': blocked,
        'examples': prompt_engine.suggested_questions(modules),
        'configured': configured,
        'is_local': is_local,
        'borrowed': borrowed,
        'borrowed_from': candidates[0].get_purpose_display() if borrowed else '',
        'provider': candidates[0].provider if candidates else '',
        'provider_label': candidates[0].get_provider_display() if candidates else '',
        'model': candidates[0].model if candidates else '',
        'limits': {
            'rounds': policy.max_tool_rounds,
            'tokens': policy.max_total_tokens,
            'per_10min': policy.rate_limit_per_10min,
        },
    })


@require_POST
@perm_required('copilot', 'read')
def ask(request):
    body = _body(request)
    question = (body.get('question') or '').strip()
    if len(question) < 2:
        return _error('EMPTY_QUESTION', '请输入完整的问题')
    question = question[:MAX_QUESTION]

    policy = CopilotPolicy.current()
    verdict = security.check_rate_limit(request.user, policy)
    if not verdict.allowed:
        return _error(
            'RATE_LIMITED',
            f'请求过于频繁，请 {verdict.retry_after // 60 or 1} 分钟后再试',
            status=429, retry_after=verdict.retry_after,
        )

    conversation = None
    conversation_id = body.get('conversation_id')
    if conversation_id:
        conversation = CopilotConversation.objects.filter(
            pk=conversation_id, user=request.user).first()
    if conversation is None:
        conversation = CopilotConversation.objects.create(
            user=request.user, title=question[:60])

    # 幂等：同会话已有进行中的 run，直接复用，避免连点造成并发调用
    active = CopilotRun.objects.filter(
        conversation=conversation,
        status__in=[CopilotRun.Status.PENDING, CopilotRun.Status.RUNNING],
    ).first()
    if active:
        data = _run_data(active)
        data.update({'sync': False, 'reused': True})
        return _payload(data)

    try:
        with transaction.atomic():
            run = CopilotRun.objects.create(
                conversation=conversation, user=request.user, question=question,
                deadline_at=timezone.now() + timedelta(seconds=_run_timeout()),
            )
            CopilotMessage.objects.create(
                conversation=conversation, role=CopilotMessage.Role.USER,
                content=question,
            )
    except IntegrityError:
        # 并发提交撞上部分唯一索引：返回对方创建的 run
        active = CopilotRun.objects.filter(
            conversation=conversation,
            status__in=[CopilotRun.Status.PENDING, CopilotRun.Status.RUNNING],
        ).first()
        if active:
            data = _run_data(active)
            data.update({'sync': False, 'reused': True})
            return _payload(data)
        return _error('CONFLICT', '提交冲突，请重试', status=409)

    if getattr(settings, 'COPILOT_INLINE_EXECUTION', False):
        tasks.execute_sync(run.pk)
        run.refresh_from_db()
        data = _run_data(run)
        data['sync'] = True
        return _payload(data)

    tasks.submit(run.pk)
    data = _run_data(run)
    data['sync'] = False
    return _payload(data, status=202)


@require_GET
@perm_required('copilot', 'read')
def run_detail(request, run_id):
    run = get_object_or_404(CopilotRun, pk=run_id, user=request.user)
    if not run.is_terminal:
        _maybe_reap()
        run.refresh_from_db()
    return _payload(_run_data(run))


@require_POST
@perm_required('copilot', 'read')
def cancel(request, run_id):
    run = get_object_or_404(CopilotRun, pk=run_id, user=request.user)
    if not run.is_terminal:
        CopilotRun.objects.filter(pk=run.pk).update(
            status=CopilotRun.Status.CANCELLED,
            finished_at=timezone.now(), answer='已取消',
            error_code='CANCELLED',
        )
        run.refresh_from_db()
    return _payload(_run_data(run))


def _conversation_data(conversation, limit=80):
    """会话 + 消息（供 history / latest 共用）。

    只回正文、引用、步骤与时间：工具原始结果不落前端，避免把服务端查询细节
    暴露到浏览器；引用链接由服务端生成，前端只做渲染。
    """
    rows = list(conversation.messages.order_by('-id')[:limit])
    return {
        'conversation_id': conversation.pk,
        'title': conversation.title,
        'messages': [
            {
                'id': m.pk,
                'role': m.role,
                'content': m.content,
                'content_html': (markdown_render.render(m.content)
                                 if m.role == 'assistant' else ''),
                'refs': m.refs,
                'steps': m.tool_calls,
                'error_code': m.error_code,
                'created_at': m.created_at.strftime('%Y-%m-%d %H:%M'),
            }
            for m in reversed(rows)
        ],
    }


@require_GET
@perm_required('copilot', 'read')
def latest_conversation(request):
    """最近一次会话（跨页面恢复用）。

    面板嵌在每个页面里，页面一跳转前端状态就没了。这里按 ``last_active_at``
    取该用户最近一条会话，前端打开面板时自动接上，用户看到的是"接着聊"，
    而不是每换一个页面就重新开始。
    """
    conversation = (CopilotConversation.objects
                    .filter(user=request.user)
                    .order_by('-last_active_at', '-id')
                    .first())
    if conversation is None:
        return _payload({'conversation_id': None, 'title': '', 'messages': []})
    return _payload(_conversation_data(conversation, limit=40))


@require_GET
@perm_required('copilot', 'read')
def history(request, pk):
    conversation = get_object_or_404(CopilotConversation, pk=pk, user=request.user)
    return _payload(_conversation_data(conversation))


@require_POST
@perm_required('copilot', 'read')
def feedback(request):
    body = _body(request)
    message = CopilotMessage.objects.filter(
        pk=body.get('message_id'), conversation__user=request.user,
    ).first()
    if message is None:
        return _error('NOT_FOUND', '消息不存在', status=404)
    try:
        vote = int(body.get('vote'))
    except (TypeError, ValueError):
        return _error('BAD_VOTE', '评价取值必须是 -1 或 1')
    if vote not in (-1, 1):
        return _error('BAD_VOTE', '评价取值必须是 -1 或 1')

    CopilotFeedback.objects.update_or_create(
        message=message, user=request.user,
        defaults={'vote': vote, 'comment': str(body.get('comment') or '')[:500]},
    )
    return _payload({'message_id': message.pk, 'vote': vote})
