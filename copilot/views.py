"""Copilot 接口：bootstrap / ask / run 轮询 / cancel / history / feedback。

约定（与 docs/api-conventions.md 一致）：
- 成功 ``{"ok": true, "data": {...}}``；失败 ``{"ok": false, "error": CODE, "message": "..."}``；
- 所有视图都挂 ``@perm_required('copilot', 'read')``，数据按 ``request.user`` 隔离；
- 前端 fetch 带 ``X-Requested-With: XMLHttpRequest``，权限失败返回 JSON 403 而不是跳转页面。
"""

import json
import logging
import queue
import time
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from accounts.decorators import perm_required
from accounts.services import has_perm
from work_record.export_utils import content_disposition

from . import export as export_mod
from . import markdown_render, registry, security, streaming, tasks
from .engine import prompt as prompt_engine
from .models import (CopilotConversation, CopilotFeedback, CopilotMessage,
                     CopilotPolicy, CopilotRun)
from .providers import router

logger = logging.getLogger(__name__)

MAX_QUESTION = 500
MAX_TITLE = 120
CONVERSATION_PAGE = 20
REAP_THROTTLE_SECONDS = 10
# 点踩原因白名单：不接受前端任意字符串，否则周报里的分类统计会被写成自由文本
FEEDBACK_REASONS = {code for code, _label in CopilotFeedback.Reason.choices}


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
    message_id = None
    if run.is_terminal:
        # 反馈要挂在"这条回答"上而不是 run 上：消息才是会话里能被点到的实体。
        message_id = (CopilotMessage.objects
                      .filter(conversation_id=run.conversation_id,
                              role=CopilotMessage.Role.ASSISTANT)
                      .order_by('-id')
                      .values_list('pk', flat=True).first())
    return {
        'run_id': str(run.pk),
        'conversation_id': run.conversation_id,
        'message_id': message_id,
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
        # 前端据此决定要不要显示「存草稿」：没权限就别给按钮，点了也失败
        'can_save_draft': has_perm(request.user, 'knowledge', 'create'),
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


@require_GET
@perm_required('copilot', 'read')
def run_stream(request, run_id):
    """SSE：边执行边推 token / step，结束后推 done。

    与 ``/run/<id>/``（轮询）并存，不是替换：多进程部署时事件总线只在
    本进程有效，前端拿不到事件就必须能退回轮询。
    """
    run = get_object_or_404(CopilotRun, pk=run_id, user=request.user)

    def done_event(status, error_code=''):
        return {'run_id': str(run.pk), 'status': status, 'error_code': error_code}

    def generate():
        if run.is_terminal:
            # 已经跑完：直接告诉前端"去取结果"，不用再等
            yield streaming.sse('done', done_event(run.status, run.error_code))
            return
        channel, _unsubscribe = streaming.subscribe(run.pk)
        yield ': connected\n\n'
        deadline = time.time() + _run_timeout() + 15
        idle_ticks = 0
        while time.time() < deadline:
            try:
                item = channel.queue.get(timeout=1)
            except queue.Empty:
                idle_ticks += 1
                if idle_ticks % 10 == 0:
                    _maybe_reap()
                    run.refresh_from_db(fields=['status', 'error_code'])
                    if run.is_terminal:
                        yield streaming.sse('done', done_event(run.status, run.error_code))
                        return
                if idle_ticks % 15 == 0:
                    yield ': keepalive\n\n'   # 让代理/浏览器别把连接判死
                continue
            if item is None:
                return
            event, data = item
            yield streaming.sse(event, data)
            if event in ('done', 'error'):
                return
        yield streaming.sse('done', done_event('timeout', 'TIMEOUT'))

    response = StreamingHttpResponse(generate(), content_type='text/event-stream')
    response['Cache-Control'] = 'no-cache, no-transform'
    # nginx 默认会把响应缓冲起来，流式就白做了
    response['X-Accel-Buffering'] = 'no'
    return response


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


RESTRICTED_ANSWER = ('这条回答涉及你已无权访问的模块，正文已隐藏。'
                     '如需查看，请联系管理员恢复对应模块的查看权限。')


def _visible_module_keys(user):
    """当前仍有读权限的模块（用本地口径，外发策略与此无关）。"""
    try:
        policy = CopilotPolicy.current()
        return {m.key for m in registry.available_modules(user, True, policy)}
    except Exception:  # noqa: BLE001 - 权限服务异常时按"全不可见"处理更安全
        logger.exception('读取模块权限失败')
        return set()


def _conversation_data(conversation, limit=80, user=None):
    """会话 + 消息（供 history / latest 共用）。

    只回正文、引用、步骤与时间：工具原始结果不落前端，避免把服务端查询细节
    暴露到浏览器；引用链接由服务端生成，前端只做渲染。

    **按当前权限复核**：历史回答是"当时有权限"时生成的，权限被收回后不能
    继续把数据摆在那里。这里只做读时判定，不回写数据——权限变更频繁，
    批量改历史消息的代价远大于每次读取时算一遍。
    """
    user = user or conversation.user
    visible = _visible_module_keys(user)
    rows = list(conversation.messages.order_by('-id')[:limit])
    rows = list(reversed(rows))
    votes = {fb.message_id: fb.vote for fb in CopilotFeedback.objects.filter(
        user=user, message__in=rows)}
    messages = []
    for m in rows:
        refs = m.refs or []
        denied = [r for r in refs if r.get('module') and r['module'] not in visible]
        restricted = bool(denied) and m.role == CopilotMessage.Role.ASSISTANT
        content = RESTRICTED_ANSWER if restricted else m.content
        messages.append({
            'id': m.pk,
            'role': m.role,
            'content': content,
            'content_html': (markdown_render.render(content)
                             if m.role == 'assistant' else ''),
            'refs': [] if restricted else refs,
            'steps': [] if restricted else m.tool_calls,
            'error_code': m.error_code,
            'restricted': restricted,
            'restricted_modules': sorted({r['module'] for r in denied}),
            'vote': votes.get(m.pk, 0),
            'created_at': m.created_at.strftime('%Y-%m-%d %H:%M'),
        })
    return {
        'conversation_id': conversation.pk,
        'title': conversation.title,
        'messages': messages,
    }


@require_GET
@perm_required('copilot', 'read')
def latest_conversation(request):
    """最近一次会话（跨页面恢复用）。

    面板嵌在每个页面里，页面一跳转前端状态就没了。这里按 ``last_active_at``
    取该用户最近一条会话，前端打开面板时自动接上，用户看到的是"接着聊"，
    而不是每换一个页面就重新开始。已归档/已删除的会话不参与。
    """
    conversation = (CopilotConversation.objects
                    .filter(user=request.user,
                            status=CopilotConversation.Status.ACTIVE)
                    .order_by('-last_active_at', '-id')
                    .first())
    if conversation is None:
        return _payload({'conversation_id': None, 'title': '', 'messages': []})
    return _payload(_conversation_data(conversation, limit=40))


@require_GET
@perm_required('copilot', 'read')
def conversations(request):
    """会话列表（「历史」抽屉）：最近的若干会话，带消息数与首问标题。"""
    rows = (CopilotConversation.objects
            .filter(user=request.user, status=CopilotConversation.Status.ACTIVE)
            .annotate(message_count=Count('messages'))
            .order_by('-last_active_at', '-id')[:CONVERSATION_PAGE])
    return _payload({
        'limit': CONVERSATION_PAGE,
        'items': [
            {
                'id': c.pk,
                'title': c.title or '新会话',
                'message_count': c.message_count,
                'last_active_at': timezone.localtime(c.last_active_at).strftime('%m-%d %H:%M'),
                'created_at': timezone.localtime(c.created_at).strftime('%Y-%m-%d'),
                'token_total': c.token_total,
            }
            for c in rows
        ],
    })


@require_POST
@perm_required('copilot', 'read')
def rename_conversation(request, pk):
    conversation = get_object_or_404(
        CopilotConversation, pk=pk, user=request.user,
        status=CopilotConversation.Status.ACTIVE)
    title = ' '.join(str(_body(request).get('title') or '').split())
    if not title:
        return _error('EMPTY_TITLE', '标题不能为空')
    conversation.title = title[:MAX_TITLE]
    conversation.save(update_fields=['title'])
    return _payload({'conversation_id': conversation.pk, 'title': conversation.title})


@require_POST
@perm_required('copilot', 'read')
def delete_conversation(request, pk):
    """软删会话：归档而不是物理删除，30 天后由 ``copilot_prune`` 清掉。

    误删是这类功能最常见的投诉，而库里留着一条 archived 行的成本几乎为零。
    """
    conversation = get_object_or_404(
        CopilotConversation, pk=pk, user=request.user,
        status=CopilotConversation.Status.ACTIVE)
    now = timezone.now()
    with transaction.atomic():
        CopilotRun.objects.filter(
            conversation=conversation,
            status__in=[CopilotRun.Status.PENDING, CopilotRun.Status.RUNNING],
        ).update(status=CopilotRun.Status.CANCELLED, finished_at=now,
                 error_code='CANCELLED', answer='已取消')
        conversation.status = CopilotConversation.Status.ARCHIVED
        conversation.archived_at = now
        conversation.save(update_fields=['status', 'archived_at'])
    return _payload({'conversation_id': conversation.pk, 'status': conversation.status})


@require_GET
@perm_required('copilot', 'read')
def history(request, pk):
    # 只认进行中的会话：已删除（归档）的会话不该还能被 URL 翻出来
    conversation = get_object_or_404(
        CopilotConversation, pk=pk, user=request.user,
        status=CopilotConversation.Status.ACTIVE)
    return _payload(_conversation_data(conversation))


def _own_message(request, body):
    return CopilotMessage.objects.filter(
        pk=body.get('message_id'), conversation__user=request.user,
    ).first()


def _question_before(message):
    return (CopilotMessage.objects
            .filter(conversation_id=message.conversation_id,
                    role=CopilotMessage.Role.USER, id__lt=message.id)
            .order_by('-id').first())


@require_POST
@perm_required('copilot', 'read')
def export_answer(request):
    """把这条回答引用的记录导成 Excel（每条回答一份，不是整表导出）。"""
    message = _own_message(request, _body(request))
    if message is None:
        return _error('NOT_FOUND', '消息不存在', status=404)
    workbook = export_mod.build_workbook(message, request.user)
    if workbook is None:
        return _error('NO_EXPORTABLE', '这条回答没有可导出的记录')
    stamp = timezone.localtime().strftime('%Y%m%d-%H%M')
    response = HttpResponse(
        export_mod.to_bytes(workbook),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = content_disposition(
        f'AI回答-{stamp}.xlsx', 'copilot-answer.xlsx')
    return response


@require_POST
@perm_required('copilot', 'read')
def save_draft(request):
    """把问答沉淀成知识库草稿（draft，需人工编辑后发布）。

    刻意不直接发布：AI 生成的结论没有经过人确认，一旦自动发布，错误就会
    变成"官方口径"被更多人引用。
    """
    message = _own_message(request, _body(request))
    if message is None or message.role != CopilotMessage.Role.ASSISTANT:
        return _error('NOT_FOUND', '消息不存在', status=404)
    if not has_perm(request.user, 'knowledge', 'create'):
        return _error('NO_KNOWLEDGE_PERM', '你没有知识库新建权限', status=403)

    from knowledge_base.models import KnowledgeDocument

    question = _question_before(message)
    title = ((question.content if question else 'AI 问答') or 'AI 问答').strip()[:100]
    refs = [r for r in (message.refs or []) if r.get('module')]
    lines = [
        f'## 问题\n\n{question.content if question else "（未找到原始问题）"}', '',
        f'## 结论\n\n{message.content}', '',
    ]
    if refs:
        lines.append('## 参考记录\n')
        for ref in refs[:20]:
            label = ref.get('label') or f'#{ref.get("id")}'
            module = ref.get('module_label') or ref.get('module') or ''
            url = ref.get('url') or ''
            lines.append(f'- {module}：{label}' + (f'（{url}）' if url else ''))
    summary = markdown_render.to_plain(message.content)[:200]

    document = KnowledgeDocument.objects.create(
        title=title or 'AI 问答草稿',
        content='\n'.join(lines),
        summary=summary,
        status='draft',
        owner=request.user,
        visibility='private',
        source='copilot',
    )
    try:
        from records.audit import write_operation
        write_operation(
            request.user.get_full_name() or request.user.username,
            'AI 助手', 'AI 生成草稿', f'knowledge:{document.pk}',
            title[:80], operator_username=request.user.username,
        )
    except Exception:  # noqa: BLE001 - 留痕失败不影响草稿本身
        logger.exception('AI 草稿留痕失败')
    return _payload({
        'document_id': document.pk,
        'title': document.title,
        'url': f'/knowledge/document/{document.pk}/edit/',
    })


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

    reason = str(body.get('reason') or '').strip()
    if reason and reason not in FEEDBACK_REASONS:
        return _error('BAD_REASON', '不支持的原因分类')

    row, _created = CopilotFeedback.objects.update_or_create(
        message=message, user=request.user,
        defaults={'vote': vote, 'reason': reason,
                  'comment': str(body.get('comment') or '')[:500]},
    )
    return _payload({'message_id': message.pk, 'vote': vote, 'reason': row.reason})
