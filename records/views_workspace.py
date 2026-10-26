"""待办工作台：跨模块聚合「我的待办」，统一归一为一种行。

口径说明：只收录「还需要人推进」的事项——已终止的试样是终态、不需要跟进，
不作为待办来源（终止提醒由消息通知承担）。

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
    "daily_record": {"module": "daily_record", "label": "每日记录"},
    "knowledge_todo": {"module": "knowledge", "label": "知识待办"},
}

URGENCY_RANK = {"高": 0, "中": 1, "低": 2}
URGENCY_BADGE = {"高": "danger", "中": "warning", "低": "info"}

# 每来源最多聚合条数（超出部分到对应模块列表页处理）
PER_SOURCE_LIMIT = 50
# 证照临期窗口（天）
LICENSE_LEAD_DAYS = 30
# 证照逾期回溯窗口（天）：超出的陈年证照不再常驻待办，改到证照列表「已过期」查看
LICENSE_LOOKBACK_DAYS = 90


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


def _source_matches(source, source_filter):
    """source_filter 既支持 source key（如 'license_due'）也支持 module 名（如 'archive'）。"""
    if source_filter in (None, ""):
        return True
    if source == source_filter:
        return True
    if SOURCE_META.get(source, {}).get("module") == source_filter:
        return True
    return False


def collect_todos(user, *, today=None, source_filter=None):
    """聚合当前用户有权读取的全部待办，按 紧急度, 期限(空值最后), 来源 排序。"""
    today = today or timezone.localdate()
    todos = []

    def wanted(source):
        return _source_matches(source, source_filter)

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

    # 2) 证照：一张证照只出一条待办（有效期/年审取更早的一项）
    if wanted("license_due") and has_perm(user, "archive", "read"):
        from django.db.models import Q

        from archive.models import License

        threshold = today + timedelta(days=LICENSE_LEAD_DAYS)
        floor = today - timedelta(days=LICENSE_LOOKBACK_DAYS)
        cond = Q(expire_date__gte=floor, expire_date__lte=threshold)
        # 年审只在设置了年审周期时才提醒：review_cycle=0 表示不需年审，
        # 历史导入残留的 next_review_date 不应生成脏提醒
        cond |= Q(
            next_review_date__gte=floor, next_review_date__lte=threshold,
            review_cycle__gt=0,
        )
        for lic in License.objects.filter(cond)[:PER_SOURCE_LIMIT]:
            checks = []
            if lic.expire_date and floor <= lic.expire_date <= threshold:
                checks.append((lic.expire_date, "有效期", "已过期", "临期"))
            if (lic.review_cycle and lic.next_review_date
                    and floor <= lic.next_review_date <= threshold):
                checks.append((lic.next_review_date, "年审", "年审超期", "年审临期"))
            if not checks:
                continue
            deadline, kind, status_overdue, status_soon = min(checks, key=lambda c: c[0])
            overdue = deadline < today
            if kind == "有效期":
                title = f"证照《{lic.name}》（{lic.code}）有效期{'已过' if overdue else '将至'}"
            else:
                title = f"证照《{lic.name}》（{lic.code}）年审{'已超期' if overdue else '临期'}"
            todos.append(_todo(
                "高" if overdue else "中", title, "license_due",
                lic.holder or "-", deadline,
                status_overdue if overdue else status_soon,
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

    # 5) 每日记录（按用户隔离）：逾期未完成高、3 天内到期中、其余低
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

    # 6) 知识待办：大纲待办节点；可见性过滤必须走 access.doc_visibility_q
    # （公开+本人文档，他人私有文档里的待办绝不出现）
    if wanted("knowledge_todo") and has_perm(user, "knowledge", "read"):
        from knowledge_base.access import doc_visibility_q
        from knowledge_base.models import KnowledgeNode

        threshold = today + timedelta(days=7)
        nodes = (KnowledgeNode.objects
                 .filter(node_type="todo", is_done=False,
                         due_date__isnull=False, due_date__lte=threshold)
                 .filter(doc_visibility_q(user, "document__"))
                 .exclude(document__status="draft")
                 .select_related("document")[:PER_SOURCE_LIMIT])
        for n in nodes:
            overdue = n.due_date < today
            doc_title = n.document.title
            text = n.text if len(n.text) <= 40 else n.text[:40] + "…"
            todos.append(_todo(
                "高" if overdue else "中",
                f"{doc_title}：{text}",
                "knowledge_todo",
                n.document.owner.get_username() if n.document.owner else "-",
                n.due_date,
                "已逾期" if overdue else "进行中",
                f"/knowledge/document/{n.document_id}/outline/?node={n.uid}", today,
            ))

    # 期限空值排最后：用超大天数替代参与排序
    todos.sort(key=lambda t: (
        URGENCY_RANK[t["urgency"]],
        t["days_left"] if t["days_left"] is not None else 99999,
        t["source"],
    ))
    return todos


def todo_counts(user, *, today=None):
    """行动区用的待办计数：与工作台同一份聚合，避免首页另起一套口径。"""
    todos = collect_todos(user, today=today)
    return {
        "total": len(todos),
        "urgent": sum(1 for t in todos if t["urgency"] == "高"),
        "due_soon": sum(
            1 for t in todos if t["days_left"] is not None and 0 <= t["days_left"] <= 7
        ),
    }


def _forbidden(request):
    return render(request, "accounts/403.html", {
        "required_module": "archive/customer_complaint/complaint/"
                           "daily_record/knowledge",
        "required_action": "read",
    }, status=403)


@perm_required_any(
    ("archive", "read"), ("customer_complaint", "read"), ("complaint", "read"),
    ("daily_record", "read"), ("knowledge", "read"),
)
def workspace(request):
    """待办工作台：统一待办列表 + 来源/紧急度筛选（后端分页）。"""
    user = request.user
    if not any(has_perm(user, meta["module"], "read") for meta in SOURCE_META.values()):
        return _forbidden(request)

    source_filter = request.GET.get("source") or None
    # source_filter 既可以是 SOURCE_META 的 key（如 'license_due'），
    # 也可以是 module 名（如 'archive'，用于一次匹配多个 source）。
    _valid_sources = set(SOURCE_META.keys()) | {m["module"] for m in SOURCE_META.values()}
    if source_filter not in _valid_sources:
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
    # 快捷来源标签：按业务模块名（支持一个 module 覆盖多个 source key，如 archive 同时匹配借阅+证照）
    source_shortcuts = [
        ("daily_record", "每日记录"),
        ("customer_complaint", "客诉"),
        ("complaint", "诉讼"),
        ("archive", "档案"),
        ("knowledge", "知识库"),
    ]
    return render(request, "records/workspace.html", {
        "todos": page_obj,
        "page_obj": page_obj,
        "counts": counts,
        "total": len(all_todos),
        "source_filter": source_filter,
        "urgency_filter": urgency_filter,
        "source_choices": [(k, m["label"]) for k, m in SOURCE_META.items()],
        "source_shortcuts": source_shortcuts,
        "urgency_choices": list(URGENCY_RANK.keys()),
    })
