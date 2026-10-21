"""待办工作台：跨六模块聚合「我的待办」，统一归一为一种行。

设计要点：
- 各模块口径不一，在视图层归一为 {urgency, title, source, owner, deadline,
  status, url, days_left} 八字段，模板只渲染一种行；
- 逐来源按所属模块 read 权限过滤（与到期预警同一套口径），无任何权限时 403；
- 每日记录按用户隔离（_my_daily_records），其余模块为模块级待办；
- 纯读聚合，无迁移。
"""
from datetime import timedelta

from django.core.paginator import Paginator
from django.shortcuts import render
from django.utils import timezone

from accounts.decorators import perm_required_any
from accounts.services import has_perm

from records.crud import _per_page
from records.models import Complaint, CustomerComplaint
from records.views import _my_daily_records

# 来源元数据：source key -> 所属模块 / 中文来源名
SOURCE_META = {
    "borrow_overdue": {"module": "archive", "label": "档案借阅"},
    "license_due": {"module": "archive", "label": "证照管理"},
    "customer_complaint": {"module": "customer_complaint", "label": "客户客诉"},
    "lawsuit": {"module": "complaint", "label": "诉讼"},
    "sample_terminated": {"module": "samples", "label": "试样管理"},
    "daily_record": {"module": "daily_record", "label": "每日记录"},
}

URGENCY_RANK = {"高": 0, "中": 1, "低": 2}
URGENCY_BADGE = {"高": "danger", "中": "warning", "低": "info"}

# 每来源最多聚合条数（超出部分到对应模块列表页处理）
PER_SOURCE_LIMIT = 50
# 已终止样品的回溯窗口（天），避免历史终止件长期占据待办
SAMPLE_LOOKBACK_DAYS = 90
# 证照临期窗口（天）
LICENSE_LEAD_DAYS = 30


def _todo(urgency, title, source, owner, deadline, status, url, today):
    days_left = (deadline - today).days if deadline else None
    return {
        "urgency": urgency,
        "urgency_badge": URGENCY_BADGE[urgency],
        "title": title,
        "source": source,
        "source_label": SOURCE_META[source]["label"],
        "owner": owner,
        "deadline": deadline,
        "days_left": days_left,
        "status": status,
        "url": url,
    }


def collect_todos(user, *, today=None, source_filter=None):
    """聚合当前用户有权读取的全部待办，按 紧急度, 期限(空值最后), 来源 排序。"""
    today = today or timezone.localdate()
    todos = []

    def wanted(source):
        return source_filter in (None, "", source) or source_filter == source

    # 1) 档案借阅：逾期未还为高，7 天内应还为中
    if wanted("borrow_overdue") and has_perm(user, "archive", "read"):
        from archive.models import BorrowRecord

        for b in BorrowRecord.objects.filter(
            return_time__isnull=True,
            due_time__lte=today + timedelta(days=7),
        ).select_related("archive")[:PER_SOURCE_LIMIT]:
            overdue = b.due_time < today
            todos.append(_todo(
                "高" if overdue else "中",
                f"档案《{b.archive.name}》（{b.archive.code}）应还",
                "borrow_overdue", b.borrower, b.due_time,
                "已逾期" if overdue else "借出中",
                f"/archive/archives/{b.archive_id}/", today,
            ))

    # 2) 证照：已过期为高，30 天内到期/年审为中
    if wanted("license_due") and has_perm(user, "archive", "read"):
        from archive.models import License

        threshold = today + timedelta(days=LICENSE_LEAD_DAYS)
        for lic in License.objects.filter(
            expire_date__isnull=False, expire_date__lte=threshold,
        )[:PER_SOURCE_LIMIT]:
            overdue = lic.expire_date < today
            todos.append(_todo(
                "高" if overdue else "中",
                f"证照《{lic.name}》（{lic.code}）有效期{'已过' if overdue else '将至'}",
                "license_due", lic.holder or "-", lic.expire_date,
                "已过期" if overdue else "临期",
                f"/archive/licenses/{lic.pk}/", today,
            ))
        for lic in License.objects.filter(
            next_review_date__isnull=False, next_review_date__lte=threshold,
        )[:PER_SOURCE_LIMIT]:
            overdue = lic.next_review_date < today
            todos.append(_todo(
                "高" if overdue else "中",
                f"证照《{lic.name}》（{lic.code}）年审{'已超期' if overdue else '临期'}",
                "license_due", lic.holder or "-", lic.next_review_date,
                "年审超期" if overdue else "年审临期",
                f"/archive/licenses/{lic.pk}/", today,
            ))

    # 3) 客户客诉：未完成（创建超 7 天升为高）/ 处理中为中
    if wanted("customer_complaint") and has_perm(user, "customer_complaint", "read"):
        for c in CustomerComplaint.objects.filter(
            complaint_status__in=["未完成", "处理中"],
        )[:PER_SOURCE_LIMIT]:
            age = (today - c.complaint_date).days
            urgency = "高" if (c.complaint_status == "未完成" and age > 7) else "中"
            todos.append(_todo(
                urgency,
                f"客诉：{c.customer_name}（{c.responsible_salesperson}）",
                "customer_complaint", c.responsible_salesperson, None,
                c.complaint_status,
                f"/customer_complaint/edit/{c.pk}/", today,
            ))

    # 4) 诉讼：未结案的均为待推进（模型无日期字段，期限留空排后）
    if wanted("lawsuit") and has_perm(user, "complaint", "read"):
        for l in Complaint.objects.exclude(progress="结案")[:PER_SOURCE_LIMIT]:
            todos.append(_todo(
                "中",
                f"诉讼 {l.case_number}（{l.defendant}）待推进",
                "lawsuit", l.lawyer, None, l.progress,
                f"/complaint/detail/{l.pk}/", today,
            ))

    # 5) 试样：近 90 天内终止的（失败靠口头传递的缺口，先做到对人可见）
    if wanted("sample_terminated") and has_perm(user, "samples", "read"):
        from samples.models import Sample

        cutoff = timezone.now() - timedelta(days=SAMPLE_LOOKBACK_DAYS)
        for s in Sample.objects.filter(
            status=Sample.Status.TERMINATED, is_deleted=False,
            terminated_at__gte=cutoff,
        )[:PER_SOURCE_LIMIT]:
            todos.append(_todo(
                "低",
                f"样品 {s.sample_no}（{s.material_name}/{s.model}）已终止",
                "sample_terminated", s.terminated_by or s.registrar or "-",
                None, "已终止",
                f"/samples/{s.pk}/", today,
            ))

    # 6) 每日记录（按用户隔离）：逾期未完成高、3 天内到期中、其余低
    if wanted("daily_record") and has_perm(user, "daily_record", "read"):
        for r in _my_daily_records(user).filter(status="未完成")[:PER_SOURCE_LIMIT]:
            if r.due_date and r.due_date < today:
                urgency = "高"
            elif r.due_date and r.due_date <= today + timedelta(days=3):
                urgency = "中"
            else:
                urgency = "低"
            title = r.work_content if len(r.work_content) <= 50 else r.work_content[:50] + "…"
            todos.append(_todo(
                urgency, title, "daily_record", r.department, r.due_date,
                "未完成", f"/daily_record/detail/{r.pk}/", today,
            ))

    # 期限空值排最后：用超大天数替代参与排序
    todos.sort(key=lambda t: (
        URGENCY_RANK[t["urgency"]],
        t["days_left"] if t["days_left"] is not None else 99999,
        t["source"],
    ))
    return todos


def _forbidden(request):
    return render(request, "accounts/403.html", {
        "required_module": "archive/customer_complaint/complaint/samples/daily_record",
        "required_action": "read",
    }, status=403)


@perm_required_any(
    ("archive", "read"), ("customer_complaint", "read"), ("complaint", "read"),
    ("samples", "read"), ("daily_record", "read"),
)
def workspace(request):
    """待办工作台：统一待办列表 + 来源/紧急度筛选（后端分页）。"""
    user = request.user
    if not any(has_perm(user, meta["module"], "read") for meta in SOURCE_META.values()):
        return _forbidden(request)

    source_filter = request.GET.get("source") or None
    if source_filter not in SOURCE_META:
        source_filter = None
    urgency_filter = request.GET.get("urgency") or None
    if urgency_filter not in URGENCY_RANK:
        urgency_filter = None

    todos = collect_todos(user, source_filter=source_filter)
    if urgency_filter:
        todos = [t for t in todos if t["urgency"] == urgency_filter]

    # 页头统计基于未筛选集合（保持口径稳定）
    all_todos = collect_todos(user) if (source_filter or urgency_filter) else todos
    counts = {"高": 0, "中": 0, "低": 0}
    for t in all_todos:
        counts[t["urgency"]] += 1

    page_obj = Paginator(todos, _per_page(request)).get_page(request.GET.get("page"))
    return render(request, "records/workspace.html", {
        "todos": page_obj,
        "page_obj": page_obj,
        "counts": counts,
        "total": len(all_todos),
        "source_filter": source_filter,
        "urgency_filter": urgency_filter,
        "source_choices": [(k, m["label"]) for k, m in SOURCE_META.items()],
        "urgency_choices": list(URGENCY_RANK.keys()),
    })
