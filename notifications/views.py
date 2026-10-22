"""通知中心视图：列表 / 已读 / 撤回 / 未读接口 / 偏好设置 / 公告推送 / 定时任务。

权限码（registry: notifications 模块为只读）：
- notifications.read：通知中心、未读接口、偏好设置、定时任务展示（行级校验保证仅本人数据）
- system.create：系统公告推送（管理员专属，staff 默认不授予）
- system.update：撤回（仅触发人本人或超管，行级校验兜底）
- 定时任务配置/手动执行：仅超级管理员（页面仅超管可见配置入口）
"""
import datetime
import functools

from django.conf import settings
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.decorators import perm_required

from . import scheduler, services
from .models import Notification, ScheduledTask, TEMPLATE_CATEGORIES


def _is_ajax(request):
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


def _superuser_required(view_func):
    """定时任务配置/执行仅超级管理员可用。"""
    @functools.wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        user = getattr(request, "user", None)
        if not getattr(user, "is_authenticated", False):
            if _is_ajax(request):
                return JsonResponse({"ok": False, "message": "请先登录"}, status=401)
            from urllib.parse import quote

            return redirect(f"{settings.LOGIN_URL}?next={quote(request.get_full_path(), safe='/?&=')}")
        if not user.is_superuser:
            if _is_ajax(request):
                return JsonResponse({"ok": False, "message": "仅超级管理员可操作"}, status=403)
            return render(request, "accounts/403.html", status=403)
        return view_func(request, *args, **kwargs)
    return _wrapped


def _per_page(request, default=10):
    try:
        per = int(request.GET.get("per_page", default))
    except (TypeError, ValueError):
        per = default
    return min(max(per, 5), 100)


@perm_required("notifications", "read")
def notification_list(request):
    """通知中心列表：仅本人，且不展示已撤回/已过期；底部展示定时任务状态。"""
    from django.core.paginator import Paginator

    qs = services._my_notifications(request.user).exclude(
        status__in=[Notification.Status.REVOKED, Notification.Status.EXPIRED],
    )
    status_filter = request.GET.get("status", "")
    category_filter = request.GET.get("category", "")
    unread_only = request.GET.get("unread", "")
    if status_filter:
        qs = qs.filter(status=status_filter)
    if category_filter:
        qs = qs.filter(category=category_filter)
    if unread_only:
        qs = qs.filter(is_read=False)

    paginator = Paginator(qs, _per_page(request))
    page_obj = paginator.get_page(request.GET.get("page"))

    tasks_ctx = [
        {"task": task, "next_run_at": scheduler.next_run_at(task)}
        for task in ScheduledTask.objects.all()
    ]
    return render(request, "notifications/notification_list.html", {
        "page_obj": page_obj,
        "total": paginator.count,
        "unread_count": services.unread_count(request.user),
        "status_filter": status_filter,
        "category_filter": category_filter,
        "unread_only": unread_only,
        "categories": TEMPLATE_CATEGORIES,
        "now": timezone.now(),
        "scheduled_tasks": tasks_ctx,
        "worker_alive": scheduler.worker_alive(),
        "scheduler_alive": scheduler.scheduler_alive(),
    })


@perm_required("notifications", "read")
@require_POST
def notification_read(request, pk):
    """标记已读（仅本人）。"""
    notification = services._get_my_notification(request, pk)
    services.mark_read(notification)
    if _is_ajax(request):
        return JsonResponse({"ok": True})
    return redirect("notifications:list")


@perm_required("notifications", "read")
@require_POST
def notification_read_all(request):
    """全部已读（仅本人未读）。"""
    count = services.mark_all_read(request.user)
    if _is_ajax(request):
        return JsonResponse({"ok": True, "count": count})
    return redirect("notifications:list")


@perm_required("system", "update")
@require_POST
def notification_revoke(request, pk):
    """撤回：system.update 权限（或触发人本人）+ 状态规则（pending / sent未读）。"""
    from django.shortcuts import get_object_or_404

    notification = get_object_or_404(Notification, pk=pk)
    count = services.revoke(notification, request.user)
    if _is_ajax(request):
        return JsonResponse(
            {"ok": bool(count), "message": f"已撤回 {count} 条通知" if count else "该通知当前状态不可撤回"},
            status=200 if count else 422,
        )
    if count:
        messages.success(request, f"已撤回 {count} 条通知" if count > 1 else "通知已撤回")
    else:
        messages.error(request, "该通知当前状态不可撤回")
    return redirect("notifications:list")


@perm_required("notifications", "read")
def unread_api(request):
    """铃铛轮询接口：未读数 + 最近通知。"""
    qs = services._my_notifications(request.user).exclude(
        status__in=[Notification.Status.REVOKED, Notification.Status.EXPIRED],
    )
    items = [
        {
            "id": n.pk,
            "title": n.title,
            "content": n.content[:120],
            "link": n.link,
            "agg_count": n.agg_count,
            "is_read": n.is_read,
            "category": n.category,
            "category_label": n.get_category_display(),
            "created_at": timezone.localtime(n.created_at).strftime("%m-%d %H:%M"),
        }
        for n in qs[:8]
    ]
    return JsonResponse({"ok": True, "count": services.unread_count(request.user), "items": items})


@perm_required("system", "update")
def revoke_log(request):
    """撤回记录：按公告批次聚合展示发送/撤回时间与已读统计，可展开人员明细。

    可查看者 = 具备撤回权限（system.update）的管理人员。
    """
    from django.db.models import Count, Max, Q

    base = Notification.objects.filter(status=Notification.Status.REVOKED)
    items = []
    # 1) 批量公告：同 batch_id 聚合为一行（同批标题/内容一致，Max 取值即可）
    batch_rows = (
        base.exclude(batch_id="")
        .values("batch_id")
        .annotate(
            title=Max("title"),
            content=Max("content"),
            sent_at=Max("sent_at"),
            created_at=Max("created_at"),
            revoked_at=Max("updated_at"),
            total=Count("id"),
            read_count=Count("id", filter=Q(is_read=True)),
        )
        .order_by("-revoked_at")
    )
    for row in batch_rows:
        members = list(
            base.filter(batch_id=row["batch_id"])
            .select_related("recipient", "created_by")
            .order_by("is_read", "recipient__username")
        )
        items.append({
            "title": row["title"],
            "content": row["content"],
            "sent_at": row["sent_at"] or row["created_at"],
            "revoked_at": row["revoked_at"],
            "total": row["total"],
            "read_count": row["read_count"],
            "unread_count": row["total"] - row["read_count"],
            "created_by": members[0].created_by if members else None,
            "members": members,
        })
    # 2) 单条撤回（无批次）：每条一行
    for n in base.filter(batch_id="").select_related("recipient", "created_by").order_by("-updated_at"):
        items.append({
            "title": n.title,
            "content": n.content,
            "sent_at": n.sent_at or n.created_at,
            "revoked_at": n.updated_at,
            "total": 1,
            "read_count": 1 if n.is_read else 0,
            "unread_count": 0 if n.is_read else 1,
            "created_by": n.created_by,
            "members": [n],
        })
    return render(request, "notifications/revoke_log.html", {"items": items})


@perm_required("notifications", "read")
def preference_edit(request):
    """用户通知偏好：按类别开关邮件、聚合窗口、免打扰时段。"""
    prefs = services.get_prefs(request.user)
    if request.method == "POST":
        prefs.channel_prefs = {
            cat[0]: {"email": request.POST.get(f"email_{cat[0]}") == "on"}
            for cat in TEMPLATE_CATEGORIES
        }
        try:
            window = max(0, min(120, int(request.POST.get("agg_window_minutes", 5) or 0)))
        except (TypeError, ValueError):
            window = 5
        prefs.agg_window_minutes = window
        prefs.quiet_start = request.POST.get("quiet_start") or None
        prefs.quiet_end = request.POST.get("quiet_end") or None
        prefs.save()
        if _is_ajax(request):
            return JsonResponse({"ok": True, "message": "通知偏好已保存"})
        messages.success(request, "通知偏好已保存")
        return redirect("notifications:preferences")
    return render(request, "notifications/preference_form.html", {
        "prefs": prefs,
        # (key, 中文名, 该类别邮件是否开启)；模板不能带参调用 email_enabled()
        "categories": [
            (key, label, prefs.email_enabled(key))
            for key, label in TEMPLATE_CATEGORIES
        ],
    })


@perm_required("system", "create")
def announce(request):
    """管理员系统公告批量推送（全部启用用户）。"""
    if request.method == "POST":
        title = (request.POST.get("title") or "").strip()
        content = (request.POST.get("content") or "").strip()
        link = (request.POST.get("link") or "").strip()
        priority = request.POST.get("priority") or "normal"
        try:
            expires_days = int(request.POST.get("expires_days") or 0) or None
        except (TypeError, ValueError):
            expires_days = None
        errors = {}
        if not title:
            errors["title"] = "请填写公告标题"
        if not content:
            errors["content"] = "请填写公告内容"
        if errors:
            return render(request, "notifications/announce_form.html",
                          {"errors": errors, "form_data": request.POST}, status=422)
        count = services.announce(
            title, content, created_by=request.user,
            link=link, priority=priority, expires_days=expires_days,
        )
        if _is_ajax(request):
            return JsonResponse({"ok": True, "message": f"公告已推送给 {count} 位用户"})
        messages.success(request, f"公告已推送给 {count} 位用户")
        return redirect("notifications:list")
    return render(request, "notifications/announce_form.html", {
        "form_data": {},
    })


# ---- 定时任务（展示于通知中心页面，配置/执行仅超管） ----

def _parse_task_params(code, post):
    """按任务白名单解析参数；返回 (params, 错误信息)。"""
    try:
        if code == "check_expiry_notices":
            days = int(post.get("days", 7))
            if not 1 <= days <= 365:
                return None, "提前提醒天数需在 1~365 之间"
            return {"days": days}, None
        if code == "check_insurance_renewal":
            days = int(post.get("days", 30))
            if not 1 <= days <= 365:
                return None, "提前提醒天数需在 1~365 之间"
            return {"days": days}, None
        if code == "check_daily_incomplete":
            return {"skip_history": post.get("skip_history") == "on"}, None
        if code == "notify_expire":
            purge_days = int(post.get("purge_days", 0))
            if not 0 <= purge_days <= 3650:
                return None, "清理天数需在 0~3650 之间（0=不清理）"
            return {"purge_days": purge_days}, None
        if code == "notify_stats":
            return {"check": post.get("check") == "on"}, None
    except (TypeError, ValueError):
        return None, "参数格式不正确"
    return {}, None


def _task_summary(message, limit=120):
    """取执行输出首行作为摘要。"""
    for line in (message or "").splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return "无输出"


@_superuser_required
@require_POST
def scheduled_task_update(request, pk):
    """超管配置定时任务：启用开关 / 触发方式 / 执行时间 / 任务参数。"""
    task = get_object_or_404(ScheduledTask, pk=pk)
    errors = []
    enabled = request.POST.get("enabled") == "on"
    trigger_type = request.POST.get("trigger_type")
    run_time = None
    interval_minutes = None

    if trigger_type == ScheduledTask.TriggerType.DAILY:
        raw = (request.POST.get("run_time") or "").strip()
        try:
            run_time = datetime.time.fromisoformat(raw)
        except ValueError:
            errors.append("请选择有效的每日执行时间")
    elif trigger_type == ScheduledTask.TriggerType.INTERVAL:
        try:
            interval_minutes = int(request.POST.get("interval_minutes", 0))
        except (TypeError, ValueError):
            interval_minutes = 0
        if not 1 <= interval_minutes <= 7 * 24 * 60:
            errors.append("间隔分钟数需在 1~10080 之间")
    else:
        errors.append("触发方式无效")
        trigger_type = None

    params, param_error = _parse_task_params(task.code, request.POST)
    if param_error:
        errors.append(param_error)

    if errors:
        if _is_ajax(request):
            return JsonResponse({"ok": False, "message": "；".join(errors)}, status=422)
        messages.error(request, "；".join(errors))
        return redirect("notifications:list")

    task.enabled = enabled
    task.trigger_type = trigger_type
    task.run_time = run_time
    task.interval_minutes = interval_minutes
    task.params = params
    task.save(update_fields=[
        "enabled", "trigger_type", "run_time", "interval_minutes", "params", "updated_at",
    ])
    if _is_ajax(request):
        return JsonResponse({"ok": True, "message": f"「{task.name}」配置已保存"})
    messages.success(request, f"「{task.name}」配置已保存")
    return redirect("notifications:list")


@_superuser_required
@require_POST
def scheduled_task_run(request, pk):
    """超管手动立即执行一次定时任务（与调度线程共用原子领取，防并发重复）。"""
    task = get_object_or_404(ScheduledTask, pk=pk)
    executed, ok, message = scheduler.run_task(task, source="manual")
    if not executed:
        if _is_ajax(request):
            return JsonResponse({"ok": False, "message": message}, status=422)
        messages.error(request, message)
        return redirect("notifications:list")
    summary = f"「{task.name}」执行{'成功' if ok else '失败'}：{_task_summary(message)}"
    if _is_ajax(request):
        return JsonResponse({"ok": bool(ok), "message": summary}, status=200 if ok else 422)
    (messages.success if ok else messages.error)(request, summary)
    return redirect("notifications:list")
