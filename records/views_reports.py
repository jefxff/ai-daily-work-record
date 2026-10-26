"""每日记录 → 个人报告：周报生成 / 年度总结。

数据隔离：一律基于 `records.views._my_daily_records`（仅本人），
不提供跨用户（部门）范围，避免破坏「每日记录按用户隔离」的硬约束。
周报 / 年报以模板生成为主；若「系统 → AI 配置」已启用，则调用配置的模型润色，
调用失败自动回退到模板文本，保证 AI 不可用不影响功能可用性。
"""
import logging
from datetime import datetime, timedelta

from django.db.models import Avg, Count
from django.db.models.functions import TruncMonth
from django.shortcuts import render
from django.utils import timezone

from accounts.decorators import perm_required
from ai_api.client import polish_text
from ai_api.models import AiProviderConfig
from work_record.http_utils import parse_date as _parse_date  # 统一实现（复核报告 CODE-01）

from .views import _my_daily_records

logger = logging.getLogger(__name__)

MAX_SNIPPET = 200

WEEKLY_POLISH_INSTRUCTION = (
    "你是一名严谨的职场助理。请在完全保留事实、数字与项目条目的前提下，"
    "把下面的周报 Markdown 润色得更通顺、更专业；保持原有 Markdown 结构与标题层级，"
    "不得新增或删减任何事实信息。只输出润色后的 Markdown 正文。"
)
ANNUAL_POLISH_INSTRUCTION = (
    "你是一名严谨的职场助理。请把下面的个人年度总结要点润色得更凝练、更有感染力；"
    "必须原样保留所有数字与统计口径，不得新增或删减事实。只输出润色后的正文。"
)


def ai_enabled():
    """AI 润色是否已启用（供模板提示）。"""
    try:
        config = AiProviderConfig.current()
    except Exception:
        return False
    return bool(config and config.is_enabled)


def maybe_ai_polish(text, instruction):
    """按当前 AI 配置润色文本；未启用或调用失败时原样返回模板文本。"""
    try:
        polished = polish_text(text, instruction)
    except Exception:
        logger.exception("AI 润色异常，回退模板文本")
        return text
    return polished or text


def _clamp_year(raw):
    current = timezone.localdate().year
    try:
        year = int(raw)
    except (TypeError, ValueError):
        return current
    return max(2000, min(current, year))


def _snippet(text):
    """工作内容压成单行并截断，便于放入周报段落。"""
    flat = " ".join((text or "").split())
    return flat[:MAX_SNIPPET] + "…" if len(flat) > MAX_SNIPPET else flat


def _md_line(record):
    return f"{record.department}｜{_snippet(record.work_content)}（进度 {record.progress}%）"


def _build_weekly_markdown(week_start, week_end, completed, ongoing, not_started):
    total = len(completed) + len(ongoing) + len(not_started)
    lines = [
        f"# 周报（{week_start:%Y-%m-%d} ~ {week_end:%Y-%m-%d}）",
        "",
        f"本周共记录 {total} 项，完成 {len(completed)} 项、进行中 {len(ongoing)} 项、"
        f"未完成 {len(not_started)} 项。",
        "",
    ]
    for title, group in (("已完成", completed), ("进行中", ongoing), ("未完成", not_started)):
        lines.append(f"## {title}（{len(group)}）")
        if group:
            for idx, record in enumerate(group, 1):
                lines.append(f"{idx}. {_md_line(record)}")
        else:
            lines.append("（无）")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _weekly_payload(user, start_raw=None):
    """周报数据（页面与 API 共用的唯一口径）；仅本人记录。"""
    today = timezone.localdate()
    week_start = _parse_date(start_raw)
    if week_start is None:
        week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)

    records = (
        _my_daily_records(user)
        .filter(record_date__date__range=(week_start, week_end))
        .order_by("record_date")
    )
    completed, ongoing, not_started = [], [], []
    for record in records:
        if record.status == "完成":
            completed.append(record)
        elif record.progress and record.progress > 0:
            ongoing.append(record)
        else:
            not_started.append(record)

    report_md = maybe_ai_polish(
        _build_weekly_markdown(week_start, week_end, completed, ongoing, not_started),
        WEEKLY_POLISH_INSTRUCTION,
    )
    return {
        "week_start": week_start,
        "week_end": week_end,
        "prev_start": week_start - timedelta(days=7),
        "next_start": week_start + timedelta(days=7),
        "is_current_week": week_start == today - timedelta(days=today.weekday()),
        "stats": {"total": len(completed) + len(ongoing) + len(not_started),
                  "completed": len(completed), "ongoing": len(ongoing),
                  "not_started": len(not_started)},
        "report_md": report_md,
        "ai_enabled": ai_enabled(),
    }


@perm_required("daily_record", "read")
def weekly_report(request):
    """本周（默认本周一~周日）周报生成页；仅本人记录。"""
    return render(request, "records/weekly_report.html",
                  _weekly_payload(request.user, request.GET.get("start")))


def _max_streak(days):
    """按去重升序的日期序列计算最长连续记录天数。"""
    best = cur = 0
    prev = None
    for day in days:
        cur = cur + 1 if prev is not None and (day - prev).days == 1 else 1
        best = max(best, cur)
        prev = day
    return best


def _annual_payload(user, year_raw=None):
    """年度总结数据（页面与 API 共用的唯一口径）；仅本人记录。"""
    year = _clamp_year(year_raw)
    records = _my_daily_records(user).filter(record_date__year=year)

    total = records.count()
    done = records.filter(status="完成").count()
    ongoing = total - done
    avg_progress = records.aggregate(v=Avg("progress"))["v"] or 0

    dept_rows = list(
        records.values("department").annotate(c=Count("id")).order_by("-c", "department")
    )
    days = sorted(set(records.values_list("record_date__date", flat=True)))
    active_months = (
        records.annotate(m=TruncMonth("record_date")).values("m").distinct().count()
    )
    done_rate = round(done * 100 / total, 1) if total else 0.0
    avg_value = round(avg_progress, 1)
    share_text = maybe_ai_polish("\n".join([
        f"{year} 年度工作总结",
        f"· 记录 {total} 条，完成 {done} 条（完成率 {done_rate}%）",
        f"· 平均进度 {avg_value}%",
        f"· 涉及部门 {len(dept_rows)} 个",
        f"· 覆盖月份 {active_months} 个月",
        f"· 最长连续记录 {_max_streak(days)} 天",
    ]), ANNUAL_POLISH_INSTRUCTION)

    return {
        "year": year,
        "total": total,
        "done": done,
        "ongoing": ongoing,
        "done_rate": done_rate,
        "avg_progress": avg_value,
        "department_count": len(dept_rows),
        "top_departments": dept_rows[:5],
        "max_streak": _max_streak(days),
        "active_months": active_months,
        "first_date": days[0] if days else None,
        "last_date": days[-1] if days else None,
        "years": list(range(timezone.localdate().year, 2019, -1)),
        "initial": (user.username or "U")[:1].upper(),
        "share_text": share_text,
        "ai_enabled": ai_enabled(),
    }


@perm_required("daily_record", "read")
def annual_summary(request):
    """年度个人总结卡片；仅本人记录。"""
    return render(request, "records/annual_summary.html",
                  _annual_payload(request.user, request.GET.get("year")))
