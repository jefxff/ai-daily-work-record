"""数据备份与恢复视图 —— 仅限拥有「系统管理」权限的用户访问。"""

import json
from datetime import datetime, time as dtime
from pathlib import Path

from django.http import FileResponse, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from accounts.decorators import perm_required
from . import services
from .models import BackupOperationLog


def _operator(request):
    profile = getattr(request.user, "profile", None)
    if profile and getattr(profile, "real_name", ""):
        return profile.real_name
    return request.user.username


@perm_required("system", "read")
def backup_center(request):
    """备份中心页：创建备份 / 备份包列表 / 恢复。"""
    backups, scanned_dir = services.scan_backups()
    return render(request, "sysbackup/backup_center.html", {
        "backups": backups,
        "scanned_dir": scanned_dir,
        "default_dir": services.default_dir(),
        "running": services.running_task_exists(),
    })


@require_POST
@perm_required("system", "create")
def api_start_backup(request):
    """发起全量备份（异步）。body: {dest_dir?: str}"""
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        payload = {}
    try:
        task_id = services.start_backup(
            dest_dir=(payload.get("dest_dir") or "").strip(),
            operator=_operator(request),
        )
    except (ValueError, RuntimeError) as e:
        return JsonResponse({"ok": False, "message": str(e)}, status=400)
    return JsonResponse({"ok": True, "task_id": task_id})


@require_POST
# 审计 P1-1：恢复会用备份包整体覆盖线上数据库，与 system.update（改配置/改用户）
# 不是同一风险级别，单独授权 system.restore
@perm_required("system", "restore")
def api_start_restore(request):
    """发起全量恢复（异步）。body: {path: str}"""
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        payload = {}
    path = (payload.get("path") or "").strip()
    if not path:
        return JsonResponse({"ok": False, "message": "缺少备份包路径"}, status=400)
    try:
        task_id = services.start_restore(path, operator=_operator(request))
    except (ValueError, RuntimeError) as e:
        return JsonResponse({"ok": False, "message": str(e)}, status=400)
    return JsonResponse({"ok": True, "task_id": task_id})


@require_GET
@perm_required("system", "read")
def api_status(request):
    """查询任务进度。?task=<task_id>"""
    snap = services._task_snapshot(request.GET.get("task", ""))
    if not snap:
        return JsonResponse({"ok": False, "message": "任务不存在或已过期"}, status=404)
    return JsonResponse({"ok": True, "task": snap})


@require_GET
@perm_required("system", "read")
def api_scan(request):
    """扫描目录下的备份包。?dir=<绝对路径，可空>"""
    dir_path = (request.GET.get("dir") or "").strip()
    if dir_path and not Path(dir_path).is_dir():
        return JsonResponse({"ok": False, "message": "目录不存在或不可访问"}, status=400)
    try:
        backups, scanned = services.scan_backups(dir_path or None)
    except (OSError, ValueError):
        return JsonResponse({"ok": False, "message": "目录不存在或不可访问"}, status=400)
    return JsonResponse({"ok": True, "dir": scanned, "backups": backups})


@require_POST
@perm_required("system", "delete")
def api_delete(request):
    """删除备份包。body: {path: str}"""
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        payload = {}
    try:
        name = services.delete_backup((payload.get("path") or "").strip())
    except ValueError as e:
        return JsonResponse({"ok": False, "message": str(e)}, status=400)
    # 审计 P3-3：销毁唯一备份是最敏感的动作之一，删除原来不留任何痕迹
    services._write_oplog(
        _operator(request), "删除", name, f"删除备份包 {name}",
    )
    return JsonResponse({"ok": True, "message": f"已删除 {name}"})


@require_GET
# 审计 P1-1：备份包含全库数据 + 全部附件，等于绕过所有模块 read 权限的横向导出
# 通道，system.read 只该覆盖「看健康状态/看备份列表」，下载必须单独授权 system.backup
@perm_required("system", "backup")
def download(request):
    """下载备份包。?path=<绝对路径>（必须在备份目录白名单内）

    允许 .zip 明文包与 .zip.enc 加密包（定时任务默认产出加密包，修复前
    下载按钮对加密包一律 400「备份包路径无效」导致功能失效）。
    """
    p = Path((request.GET.get("path") or "").strip())
    if not p.is_absolute() or not p.is_file() or not services.is_backup_file_name(p.name):
        return JsonResponse({"ok": False, "message": "备份包路径无效"}, status=400)
    try:
        services.ensure_path_allowed(p)
    except ValueError as e:
        return JsonResponse({"ok": False, "message": str(e)}, status=400)
    # 审计 P3-3：下载是备份体系里最敏感的读取动作，必须可回答「谁在什么时候拿走过哪包」
    services._write_oplog(
        _operator(request), "下载", p.name,
        f"下载备份包 {p.name}（{p.stat().st_size} 字节）至客户端 {request.META.get('REMOTE_ADDR', '')}",
    )
    resp = FileResponse(p.open("rb"), as_attachment=True, filename=p.name)
    return resp


@require_GET
@perm_required("system", "read")
def api_oplogs(request):
    """备份/恢复操作记录分页查询。

    ?page=1&page_size=10&op_type=backup|restore&start=YYYY-MM-DD&end=YYYY-MM-DD
    """
    try:
        page = max(1, int(request.GET.get("page", "1")))
        page_size = min(100, max(1, int(request.GET.get("page_size", "10"))))
    except ValueError:
        return JsonResponse({"ok": False, "message": "分页参数无效"}, status=400)

    start_s = (request.GET.get("start") or "").strip()
    end_s = (request.GET.get("end") or "").strip()
    try:
        qs = BackupOperationLog.objects.all()
        if start_s:
            d0 = datetime.strptime(start_s, "%Y-%m-%d").date()
            qs = qs.filter(created_at__gte=timezone.make_aware(datetime.combine(d0, dtime.min)))
        if end_s:
            d1 = datetime.strptime(end_s, "%Y-%m-%d").date()
            qs = qs.filter(created_at__lte=timezone.make_aware(datetime.combine(d1, dtime.max)))
    except ValueError:
        return JsonResponse({"ok": False, "message": "时间范围格式无效，应为 YYYY-MM-DD"}, status=400)

    # 类型徽标统计（当前时间范围内，不含类型筛选）
    stats = {
        "all": qs.count(),
        "backup": qs.filter(op_type=BackupOperationLog.OP_BACKUP).count(),
        "restore": qs.filter(op_type=BackupOperationLog.OP_RESTORE).count(),
    }

    op_type = (request.GET.get("op_type") or "").strip()
    if op_type in (BackupOperationLog.OP_BACKUP, BackupOperationLog.OP_RESTORE):
        qs = qs.filter(op_type=op_type)

    total = qs.count()
    pages = (total + page_size - 1) // page_size
    items = qs.order_by("-created_at")[(page - 1) * page_size: page * page_size]
    return JsonResponse({
        "ok": True,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "stats": stats,
        "items": [{
            "id": str(x.id),
            "op_type": x.op_type,
            "op_type_text": x.get_op_type_display(),
            "operator": x.operator,
            "file_name": x.file_name,
            "file_path": x.file_path,
            "success": x.success,
            "detail": x.detail,
            "created_at": timezone.localtime(x.created_at).strftime("%Y-%m-%d %H:%M:%S"),
        } for x in items],
    })
