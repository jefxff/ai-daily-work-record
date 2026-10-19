"""定时任务执行历史页面（system.read 只读）。

与「系统健康」页互补：健康页看最近一次执行结果，本页看时间轴上的执行轨迹
（TaskRunLog，每次执行一行，按任务裁剪至最近 200 条）。
"""
from django.core.paginator import Paginator
from django.shortcuts import render

from accounts.decorators import perm_required

from .models import ScheduledTask, TaskRunLog

PER_PAGE = 20


@perm_required("system", "read")
def task_run_history(request):
    """任务执行历史：按任务 / 执行结果筛选 + 分页（固定每页 20 条）。"""
    task_filter = request.GET.get("task", "")
    status_filter = request.GET.get("status", "")

    logs = TaskRunLog.objects.select_related("task")
    if task_filter.isdigit():
        logs = logs.filter(task_id=int(task_filter))
    else:
        task_filter = ""
    if status_filter in ("success", "fail"):
        logs = logs.filter(status=status_filter)
    else:
        status_filter = ""

    page_obj = Paginator(logs, PER_PAGE).get_page(request.GET.get("page"))
    return render(request, "notifications/task_run_history.html", {
        "page_obj": page_obj,
        "tasks": ScheduledTask.objects.all(),
        "task_filter": task_filter,
        "status_filter": status_filter,
    })
