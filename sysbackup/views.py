"""数据备份与恢复视图 —— 仅限拥有「系统管理」权限的用户访问。"""

import json
from pathlib import Path

from django.http import FileResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from accounts.decorators import perm_required
from . import services


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
@perm_required("system", "update")
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
    return JsonResponse({"ok": True, "message": f"已删除 {name}"})


@require_GET
@perm_required("system", "read")
def download(request):
    """下载备份包。?path=<绝对路径>"""
    p = Path((request.GET.get("path") or "").strip())
    if not p.is_absolute() or not p.is_file() or p.suffix.lower() != ".zip":
        return JsonResponse({"ok": False, "message": "备份包路径无效"}, status=400)
    resp = FileResponse(p.open("rb"), as_attachment=True, filename=p.name)
    return resp
