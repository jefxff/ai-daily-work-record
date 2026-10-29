"""运行日志数据层：``logs/`` 目录增量采集 + 结构化解析 + 聚合统计。

设计要点：
- 只采集 ``settings.LOG_INGEST_FILES`` 白名单（glob）内的文件，且必须落在
  ``BASE_DIR/logs`` 目录内；真实路径带符号链接时按 realpath 校验，杜绝目录穿越。
- 每个文件用 ``.log_ingest_state.json`` 记录已读字节偏移，只读新增内容；
  文件变小视为轮转/截断，偏移归零重读。重复内容由 ``SystemLog.fingerprint``
  唯一约束 + ``bulk_create(ignore_conflicts=True)`` 幂等去重。
- 多行异常堆栈按「无时间戳前缀 → 归并到上一条消息」处理，单条长度截断到
  ``LOG_INGEST_MAX_MESSAGE``。
- 采集完成后按 ``LOG_RETENTION_DAYS`` / ``LOG_MAX_ROWS`` 裁剪，避免无限增长。
"""
import fnmatch
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timedelta

from django.conf import settings
from django.db.models import Count, Q
from django.utils import timezone

from .models import SystemLog

logger = logging.getLogger(__name__)

STATE_FILENAME = ".log_ingest_state.json"
BATCH_SIZE = 500
DEFAULT_DAYS = 7
MAX_DAYS = 90

_HEADER_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+"
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+"
    r"(?P<logger>\S+)\s+(?P<msg>.*)$"
)
# csp_violations.log 没有标准 logging 头，只有时间戳 + 固定前缀
_CSP_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+(?P<msg>CSP .*)$"
)
_TS_FORMAT = "%Y-%m-%d %H:%M:%S,%f"
# 敏感值掩码：password / token / secret / authorization 等键值对只保留键
_SENSITIVE_RE = re.compile(
    r"(?i)((?:password|passwd|pwd|secret|secret_key|token|authorization|api[_-]?key)[\w-]*"
    r"\s*[=:]\s*)([^\s&;,]{2,})"
)


# ============================== 文件白名单 ==============================
def log_dir():
    """日志目录（与 settings.SCHEDULER_HEARTBEAT_DIR 默认值保持一致）。"""
    return os.path.join(str(settings.BASE_DIR), "logs")


def allowed_files():
    """返回白名单内真实存在、且确实位于日志目录下的文件名（不含路径）。"""
    base = os.path.realpath(log_dir())
    if not os.path.isdir(base):
        return []
    names = []
    try:
        entries = os.listdir(base)
    except OSError as exc:
        logger.warning("读取日志目录失败：%s", exc)
        return []
    patterns = [str(p) for p in getattr(settings, "LOG_INGEST_FILES", ())]
    patterns = [p for p in patterns if p and "/" not in p and "\\" not in p]
    for entry in entries:
        if not any(fnmatch.fnmatchcase(entry.lower(), p.lower()) for p in patterns):
            continue
        path = os.path.realpath(os.path.join(base, entry))
        if os.path.isfile(path) and os.path.dirname(path) == base:
            names.append(entry)
    return sorted(set(names))


def describe_files():
    """纳管文件清单：大小 / 最后写入时间 / 已采集中断点，供页面展示与排障。"""
    state = _load_state()
    items = []
    for name in allowed_files():
        path = os.path.join(log_dir(), name)
        try:
            stat = os.stat(path)
        except OSError:
            continue
        items.append({
            "name": name,
            "size": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.get_current_timezone()),
            "offset": int((state.get(name) or {}).get("offset") or 0),
        })
    return items


# ============================== 采集状态 ==============================
def _state_path():
    return os.path.join(log_dir(), STATE_FILENAME)


def _load_state():
    try:
        with open(_state_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(state):
    path = _state_path()
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as exc:
        # 状态文件写不进去不影响已入库数据；下次重读只是多一次去重
        logger.warning("写入日志采集状态失败：%s", exc)


# ============================== 解析 ==============================
def _mask_sensitive(text):
    if not getattr(settings, "LOG_MASK_SENSITIVE", True):
        return text
    return _SENSITIVE_RE.sub(lambda m: m.group(1) + "***", text)


def _make_entry(ts, level, source, logger_name, message):
    try:
        naive = datetime.strptime(ts, _TS_FORMAT)
    except ValueError:
        return None
    logged_at = timezone.make_aware(naive, timezone.get_current_timezone())
    message = _mask_sensitive(message.strip())
    max_len = int(getattr(settings, "LOG_INGEST_MAX_MESSAGE", 4000))
    if len(message) > max_len:
        message = message[:max_len] + " …"
    return {
        "logged_at": logged_at,
        "level": level,
        "logger": (logger_name or "unknown")[:120],
        "source": source[:120],
        "message": message,
    }


def _parse_chunk(chunk, source):
    """解析一段字节内容，返回（条目列表, 已消费字节数）。

    末尾不完整的行不消费，留给下一次采集，避免把半行写进库。
    """
    last_nl = chunk.rfind(b"\n")
    if last_nl < 0:
        return [], 0
    consumed = last_nl + 1
    text = chunk[:consumed].decode("utf-8", "replace")
    entries = []
    current = None
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if not line.strip():
            continue
        head = _HEADER_RE.match(line)
        csp = None if head else _CSP_RE.match(line)
        if head:
            entry = _make_entry(head.group("ts"), head.group("level"),
                                source, head.group("logger"), head.group("msg"))
        elif csp:
            entry = _make_entry(csp.group("ts"), "WARNING", source, "csp", csp.group("msg"))
        else:
            entry = None
        if entry:
            if (entry["level"] in ("DEBUG", "INFO")
                    and entry["logger"] in getattr(settings, "LOG_INGEST_SKIP_LOGGERS", ())):
                current = None
                continue
            entries.append(entry)
            current = entry
            continue
        # 续行（异常堆栈等）：并入上一条，已跳过的 logger 不再接收续行
        if current is not None and line.strip():
            max_len = int(getattr(settings, "LOG_INGEST_MAX_MESSAGE", 4000))
            if len(current["message"]) < max_len:
                current["message"] = (current["message"] + "\n" + _mask_sensitive(line)).strip()
    return entries, consumed


def _fingerprint(entry):
    raw = "|".join([
        entry["logged_at"].isoformat(),
        entry["level"],
        entry["logger"],
        entry["message"],
    ])
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()


# ============================== 采集 ==============================
def ingest_logs(max_bytes=None):
    """增量采集一次。返回统计字典供页面 / API 反馈。"""
    result = {"files": 0, "parsed": 0, "ingested": 0, "skipped": 0, "trimmed": 0}
    if not getattr(settings, "LOG_INGEST_ENABLED", True):
        return result

    base = log_dir()
    budget = max(1, int(max_bytes or getattr(settings, "LOG_INGEST_MAX_BYTES", 1048576)))
    state = _load_state()

    for name in allowed_files():
        path = os.path.join(base, name)
        try:
            stat = os.stat(path)
        except OSError:
            continue
        offset = max(0, int((state.get(name) or {}).get("offset") or 0))
        if offset > stat.st_size:  # 轮转 / 截断：从头重读，靠指纹去重
            offset = 0
        if stat.st_size <= offset:
            continue

        read_len = min(stat.st_size - offset, budget)
        try:
            with open(path, "rb") as fh:
                fh.seek(offset)
                chunk = fh.read(read_len)
        except OSError as exc:
            logger.warning("读取日志文件失败 %s：%s", name, exc)
            continue

        entries, consumed = _parse_chunk(chunk, name)
        if not consumed and read_len >= budget:
            # 超过预算仍没有换行（超长单行）：整段跳过，避免采集卡死
            consumed = read_len
        if not consumed:
            continue

        result["files"] += 1
        result["parsed"] += len(entries)
        if entries:
            rows = [
                SystemLog(
                    logged_at=entry["logged_at"],
                    level=entry["level"],
                    logger=entry["logger"],
                    source=entry["source"],
                    message=entry["message"],
                    fingerprint=_fingerprint(entry),
                )
                for entry in entries
            ]
            before = SystemLog.objects.count()
            for start in range(0, len(rows), BATCH_SIZE):
                SystemLog.objects.bulk_create(rows[start:start + BATCH_SIZE],
                                              ignore_conflicts=True)
            result["ingested"] += max(0, SystemLog.objects.count() - before)

        state[name] = {"offset": offset + consumed, "size": stat.st_size}
        _save_state(state)

    if result["ingested"] or result["parsed"]:
        result["trimmed"] = cleanup_logs()
    # 解析过但未新增的即重复内容（轮转重读 / 页面重复采集）
    result["skipped"] = max(0, result["parsed"] - result["ingested"])
    return result


def cleanup_logs():
    """按保留天数 + 行数上限裁剪，返回删除条数。"""
    deleted = 0
    days = int(getattr(settings, "LOG_RETENTION_DAYS", 30))
    if days > 0:
        cutoff = timezone.now() - timedelta(days=days)
        deleted += SystemLog.objects.filter(logged_at__lt=cutoff).delete()[0]
    max_rows = max(1, int(getattr(settings, "LOG_MAX_ROWS", 100000)))
    total = SystemLog.objects.count()
    while total > max_rows:
        limit = min(5000, total - max_rows)
        ids = list(
            SystemLog.objects.order_by("logged_at", "id")
            .values_list("id", flat=True)[:limit]
        )
        if not ids:
            break
        deleted += SystemLog.objects.filter(id__in=ids).delete()[0]
        total -= len(ids)
    if deleted:
        logger.info("日志中心裁剪 %s 条过期记录", deleted)
    return deleted


# ============================== 查询 ==============================
def clamp_days(raw, default=DEFAULT_DAYS):
    """时间窗：0 表示不限（仍受保留期约束），非法值回退默认。"""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(0, min(MAX_DAYS, value))


def apply_filters(qs, level="", source="", search="", days=DEFAULT_DAYS):
    if level:
        qs = qs.filter(level=level)
    if source:
        qs = qs.filter(source=source)
    if search:
        qs = qs.filter(Q(message__icontains=search) | Q(logger__icontains=search))
    if days:
        qs = qs.filter(logged_at__gte=timezone.now() - timedelta(days=days))
    return qs


def dashboard_stats(hours=24):
    """近 N 小时总量 / 各级别占比 / 错误来源 Top5 / 上次入库时间。"""
    since = timezone.now() - timedelta(hours=hours)
    qs = SystemLog.objects.filter(logged_at__gte=since)
    rows = qs.values("level").annotate(cnt=Count("id"))
    counts = {level: 0 for level, _label in SystemLog.LEVEL_CHOICES}
    for row in rows:
        counts[row["level"]] = row["cnt"]
    total = sum(counts.values())
    breakdown = [
        {
            "level": level,
            "label": label,
            "count": counts.get(level, 0),
            "badge": SystemLog.LEVEL_BADGES.get(level, "default"),
        }
        for level, label in SystemLog.LEVEL_CHOICES
    ]
    error_sources = list(
        qs.filter(level__in=("ERROR", "CRITICAL"))
        .values("logger")
        .annotate(cnt=Count("id"))
        .order_by("-cnt")[:5]
    )
    last_ingest = (
        SystemLog.objects.order_by("-created_at")
        .values_list("created_at", flat=True)
        .first()
    )
    return {
        "since": since,
        "total": total,
        "errors": counts.get("ERROR", 0) + counts.get("CRITICAL", 0),
        "warnings": counts.get("WARNING", 0),
        "breakdown": breakdown,
        "error_sources": error_sources,
        "last_ingest": last_ingest,
    }
