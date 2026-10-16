"""通知中心视图：列表 / 已读 / 撤回 / 未读接口 / 偏好设置 / 公告推送。

权限码（registry: notifications 模块为只读）：
- notifications.read：通知中心、未读接口、偏好设置（行级校验保证仅本人数据）
- system.create：系统公告推送（管理员专属，staff 默认不授予）
- system.update：撤回（仅触发人本人或超管，行级校验兜底）
"""
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.decorators import perm_required

from . import services
from .models import Notification, TEMPLATE_CATEGORIES


def _is_ajax(request):
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


def _per_page(request, default=15):
    try:
        per = int(request.GET.get("per_page", default))
    except (TypeError, ValueError):
        per = default
    return min(max(per, 5), 100)


@perm_required("notifications", "read")
def notification_list(request):
    """通知中心列表：仅本人，且不展示已撤回/已过期。"""
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
    params = request.GET.copy()
    params.pop("page", None)

    return render(request, "notifications/notification_list.html", {
        "page_obj": page_obj,
        "total": paginator.count,
        "unread_count": services.unread_count(request.user),
        "status_filter": status_filter,
        "category_filter": category_filter,
        "unread_only": unread_only,
        "categories": TEMPLATE_CATEGORIES,
        "query_string": params.urlencode(),
        "now": timezone.now(),
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
