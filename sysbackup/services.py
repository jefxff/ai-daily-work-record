"""数据备份与恢复核心服务。

设计要点：
- 备份包为单个 zip：db.sqlite3（数据库快照，含表结构+全部数据）+ data.sql（SQL 文本副本）
  + media/（全部附件，保持原目录结构）+ meta.json（备份元信息与附件清单）。
- 附件读写全部经 django.core.files.storage.default_storage 抽象，本地存储直接按路径打包（快），
  云存储走 storage.open 流式读写，二者均兼容。
- 数据库快照/恢复使用 SQLite 在线备份 API（sqlite3.Connection.backup），不覆盖物理文件，
  避免 Windows 下数据库文件被进程占用导致恢复失败，且不拼接任何 SQL 语句。
- 备份/恢复为后台线程任务：任务状态保存在进程内字典（单进程部署，如 runserver），
  同一时间仅允许一个备份或恢复任务运行；前端通过 status 接口轮询进度。
"""

import json
import logging
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import traceback
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.core.exceptions import SuspiciousOperation
from django.core.files.storage import default_storage
from django.utils import timezone

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1  # 备份包结构版本：恢复时强校验
DB_ENTRY = "db.sqlite3"
SQL_ENTRY = "data.sql"
META_ENTRY = "meta.json"
MEDIA_PREFIX = "media/"

# 排除在备份之外的目录（相对 MEDIA_ROOT；缓存类内容可随时再生）
MEDIA_EXCLUDE_DIRS = set()

# 默认备份目录移到项目外（用户主目录），避免备份包与代码/数据库同处一棵目录树
# 被一块拖走或误提交；可用环境变量 BACKUP_DIR 覆盖。
DEFAULT_BACKUP_DIR = Path(
    os.environ.get("BACKUP_DIR") or (Path.home() / "WorkRecordBackups")
)

# 备份包默认 Fernet 加密；密钥优先取环境变量，否则落盘到**与备份目录分离**的位置。
# 审查 S1：密钥若与备份包同目录（历史默认就是 BACKUP_DIR/.backup_key），
# 磁盘损坏 / 目录误删 / 整目录拷走时备份与密钥同时消失，历史备份永久不可解密，
# 加密本身也失去意义（拿到目录的人同时拿到密钥）。现在默认放到用户配置目录
# （Windows: %LOCALAPPDATA%\WorkRecordKeys，其它平台: ~/.workrecord），
# 与 DEFAULT_BACKUP_DIR 不再是同一棵树；BACKUP_KEY_FILE 仍可显式覆盖。
BACKUP_KEY_ENV = "BACKUP_ENCRYPTION_KEY"
_KEY_HOME = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "WorkRecordKeys"
BACKUP_KEY_FILE = Path(
    os.environ.get("BACKUP_KEY_FILE") or (_KEY_HOME / ".backup_key")
)
# 历史上出现过的密钥位置（迁移时按顺序探测，保证旧备份仍可解密）：
# 1) 项目根 .backup_key（早期版本）
# 2) 备份目录 .backup_key（v2.1 默认，审查前）
_LEGACY_BACKUP_KEY_FILES = [
    Path(settings.BASE_DIR) / ".backup_key",
    DEFAULT_BACKUP_DIR / ".backup_key",
]
# 兼容旧代码引用（早期只认项目根那一份）
_LEGACY_BACKUP_KEY_FILE = _LEGACY_BACKUP_KEY_FILES[0]
ENCRYPTED_SUFFIX = ".zip.enc"


# ---------- 路径白名单 ----------

def ensure_path_allowed(p):
    """路径白名单校验：路径必须位于 settings.BACKUP_ALLOWED_ROOTS 之一之内。

    防止 download/delete/restore/scan/backup 目标目录被指定为服务器任意路径。
    不在白名单内时抛 ValueError（视图层统一转 400）。
    """
    rp = Path(p).resolve()
    for root in getattr(settings, "BACKUP_ALLOWED_ROOTS", [str(Path(settings.BASE_DIR))]):
        try:
            rp.relative_to(Path(root).resolve())
            return
        except ValueError:
            continue
    raise ValueError(
        "路径不在允许的备份目录白名单内（如需使用项目外目录，请设置环境变量 BACKUP_ALLOWED_ROOTS）"
    )

# ---------- 任务状态（进程内缓存 + 落库） ----------
#
# 复核报告 P1-B：状态原先只存在进程内字典，重启即丢且失败无痕迹。
# 现在每次写入都同步到 sysbackup.BackupTask，进程内字典只作读缓存；
# 跨进程/重启后 _task_snapshot 会回落到数据库读取。

_STATE_LOCK = threading.Lock()
_TASKS = {}  # task_id -> dict（进程内缓存，权威数据在 BackupTask 表）
_TASK_KEEP = 50  # 最多保留最近 N 个任务记录
_RUN_SLOT = [False]  # 同一时间仅一个备份/恢复任务
_HEARTBEAT_TIMEOUT_MIN = 30  # 心跳超过该分钟数视为任务已中断（进程被杀/重启）

_TIME_FMT = "%Y-%m-%d %H:%M:%S"


def _parse_task_time(value):
    """把快照里的本地时间字符串还原为带时区的 datetime。"""
    if not value:
        return None
    try:
        dt = datetime.strptime(value, _TIME_FMT)
    except (TypeError, ValueError):
        return None
    return timezone.make_aware(dt, timezone.get_current_timezone())


def _persist(task):
    """把内存快照写回数据库；失败仅告警，不阻断备份/恢复主流程。"""
    if not task:
        return
    try:
        from .models import BackupTask

        BackupTask.objects.update_or_create(
            id=task["id"],
            defaults={
                "kind": task["kind"],
                "status": task["status"],
                "stage": task.get("stage", ""),
                "percent": task.get("percent", 0),
                "message": task.get("message", ""),
                "error": task.get("error", ""),
                "log": task.get("log", []),
                "result": task.get("result", {}),
                "started_at": _parse_task_time(task.get("started_at")) or timezone.now(),
                "finished_at": _parse_task_time(task.get("finished_at")),
                "heartbeat_at": timezone.now(),
            },
        )
    except Exception:  # 数据库被恢复回退等极端情况：内存状态仍可继续推进
        logger.warning("备份任务状态落库失败（task=%s），继续按内存状态运行", task.get("id"))


def _row_to_snapshot(row):
    """BackupTask 行 → 与原 _TASKS 完全同构的 dict（保持视图/前端契约不变）。"""
    return {
        "id": row.id,
        "kind": row.kind,
        "status": row.status,
        "stage": row.stage,
        "percent": row.percent,
        "message": row.message,
        "error": row.error,
        "log": list(row.log or []),
        "started_at": timezone.localtime(row.started_at).strftime(_TIME_FMT)
        if row.started_at else "",
        "finished_at": timezone.localtime(row.finished_at).strftime(_TIME_FMT)
        if row.finished_at else "",
        "result": dict(row.result or {}),
    }


def _task_snapshot(task_id):
    if not task_id:
        return None
    with _STATE_LOCK:
        task = _TASKS.get(task_id)
        if task:
            return dict(task)
    try:
        from .models import BackupTask

        row = BackupTask.objects.filter(pk=task_id).first()
        return _row_to_snapshot(row) if row else None
    except Exception:
        return None


def _trim_tasks():
    """数据库内只保留最近 N 个任务，避免无限增长。"""
    try:
        from .models import BackupTask

        stale_ids = list(
            BackupTask.objects.order_by("-started_at").values_list("id", flat=True)[_TASK_KEEP:]
        )
        if stale_ids:
            BackupTask.objects.filter(id__in=stale_ids).delete()
    except Exception:
        logger.debug("清理历史备份任务记录失败", exc_info=True)


def _new_task(kind, detail=""):
    task_id = uuid.uuid4().hex
    now_text = timezone.localtime().strftime(_TIME_FMT)
    with _STATE_LOCK:
        _TASKS[task_id] = {
            "id": task_id,
            "kind": kind,  # backup / restore
            "status": "running",  # running / success / failed
            "stage": "排队中",
            "percent": 0,
            "message": detail,
            "error": "",
            "log": [],
            "started_at": now_text,
            "finished_at": "",
            "result": {},
        }
        # 清理进程内历史任务，防止字典无限增长
        if len(_TASKS) > _TASK_KEEP:
            for old_id in sorted(_TASKS.keys(), key=lambda k: _TASKS[k]["started_at"])[:-_TASK_KEEP]:
                _TASKS.pop(old_id, None)
        snapshot = dict(_TASKS[task_id])
        snapshot["log"] = list(snapshot["log"])
    _persist(snapshot)
    _trim_tasks()
    return task_id


def _update(task_id, **fields):
    with _STATE_LOCK:
        task = _TASKS.get(task_id)
        if task:
            task.update(fields)
            if fields.get("status") in ("success", "failed") and not task.get("finished_at"):
                task["finished_at"] = timezone.localtime().strftime(_TIME_FMT)
            snapshot = dict(task)
            snapshot["log"] = list(snapshot["log"])
        else:
            snapshot = None
    if snapshot:
        _persist(snapshot)


def _log(task_id, message):
    line = f"[{timezone.localtime().strftime('%H:%M:%S')}] {message}"
    with _STATE_LOCK:
        task = _TASKS.get(task_id)
        if task is None:
            snapshot = None
        else:
            task["log"].append(line)
            snapshot = dict(task)
            snapshot["log"] = list(snapshot["log"])
    if snapshot:
        _persist(snapshot)
        return
    # 进程重启后恢复中的任务：日志仍要落库（行由 _update 重建）
    try:
        from .models import BackupTask

        row = BackupTask.objects.filter(pk=task_id).first()
        if row:
            row.log = list(row.log or []) + [line]
            row.heartbeat_at = timezone.now()
            row.save(update_fields=["log", "heartbeat_at"])
    except Exception:
        logger.debug("追加任务日志失败", exc_info=True)


def _progress(task_id, percent, stage=None):
    fields = {"percent": min(99, max(0, int(percent)))}
    if stage:
        fields["stage"] = stage
    _update(task_id, **fields)


def reap_stale_tasks():
    """把心跳超时的 running 任务标记为失败（进程重启/被杀留下的僵尸任务）。"""
    try:
        from datetime import timedelta

        from .models import BackupTask

        cutoff = timezone.now() - timedelta(minutes=_HEARTBEAT_TIMEOUT_MIN)
        return BackupTask.objects.filter(status="running", heartbeat_at__lt=cutoff).update(
            status="failed",
            stage="任务中断",
            error="任务被中断：进程重启或异常退出（心跳超时）",
            finished_at=timezone.now(),
            heartbeat_at=timezone.now(),
        )
    except Exception:
        logger.debug("清理僵尸备份任务失败", exc_info=True)
        return 0


def running_task_exists():
    reap_stale_tasks()
    try:
        from .models import BackupTask

        if BackupTask.objects.filter(status="running").exists():
            return True
    except Exception:
        pass
    with _STATE_LOCK:
        return any(t["status"] == "running" for t in _TASKS.values())


# ---------- 通用工具 ----------

def default_dir():
    """默认备份存储路径（项目根 backups/）。"""
    DEFAULT_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    return str(DEFAULT_BACKUP_DIR)


def _key_file() -> Path:
    """返回当前生效的密钥文件路径，必要时把旧位置的密钥迁移到新位置。

    审查 S1：新位置（用户配置目录）与备份目录分离。历史密钥可能存在于
    项目根或**备份目录**里 —— 后者尤其危险（备份与密钥同盘同目录），
    探测到就复制到新位置；复制并校验一致后删除旧副本，真正完成分离。
    任何一步失败都退回旧位置继续用（宁可少分离，也不能让历史备份解不开）。
    """
    if BACKUP_KEY_FILE.is_file():
        return BACKUP_KEY_FILE

    for legacy in _LEGACY_BACKUP_KEY_FILES:
        if legacy == BACKUP_KEY_FILE or not legacy.is_file():
            continue
        try:
            payload = legacy.read_bytes()
            BACKUP_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
            BACKUP_KEY_FILE.write_bytes(payload)
            try:
                os.chmod(BACKUP_KEY_FILE, 0o600)
            except OSError:
                pass
            if BACKUP_KEY_FILE.read_bytes() != payload:
                raise OSError("密钥复制校验失败")
            logger.warning(
                "备份密钥已从旧位置迁移：%s → %s。"
                "请确保新位置与备份目录分开备份/保管（密钥丢失将使历史加密包永久无法解密）。",
                legacy, BACKUP_KEY_FILE,
            )
            # 校验通过才删除旧副本；删不掉也不影响正确性（新位置已可用）
            try:
                legacy.unlink()
                logger.warning("旧位置密钥已删除：%s（分离完成）", legacy)
            except OSError:
                logger.warning("旧位置密钥删除失败，请手工删除：%s", legacy)
            return BACKUP_KEY_FILE
        except OSError:
            logger.warning("备份密钥迁移到 %s 失败，继续使用旧位置 %s",
                           BACKUP_KEY_FILE, legacy, exc_info=True)
            return legacy

    return BACKUP_KEY_FILE


def _fernet():
    """返回备份包**加密**用的 Fernet 实例。

    审查（2026-10-25 事故）：密钥必须能持久化，否则用完即丢 —— 现场曾出现
    「旧进程生成的密钥没写成功，包照常生成但从此无法解密」的数据丢失。
    因此这里在生成新密钥时**必须**写入成功并回读校验，失败即中止本次备份，
    宁可没有新备份，也不能产生一个永远打不开的包。
    """
    from cryptography.fernet import Fernet

    key = os.environ.get(BACKUP_KEY_ENV)
    if not key:
        key_file = _key_file()
        if key_file.is_file():
            key = key_file.read_text(encoding="utf-8").strip()
        else:
            key = Fernet.generate_key().decode("ascii")
            try:
                key_file.parent.mkdir(parents=True, exist_ok=True)
                key_file.write_text(key, encoding="utf-8")
                try:
                    os.chmod(key_file, 0o600)
                except OSError:
                    pass
                if key_file.read_text(encoding="utf-8").strip() != key:
                    raise OSError("密钥回读校验失败")
            except OSError as e:
                raise RuntimeError(
                    f"备份加密密钥无法保存到 {key_file}（{e}）。已中止本次备份："
                    f"若继续加密会产生一个永远无法解密的包。"
                    f"请检查目录权限，或显式设置环境变量 {BACKUP_KEY_ENV}。"
                ) from e
            logger.warning("已生成新的备份加密密钥：%s（请与备份目录分开保管）", key_file)
    if isinstance(key, str):
        key = key.encode("utf-8")
    try:
        return Fernet(key)
    except Exception as e:  # 非法密钥不能静默降级为明文
        raise ValueError(f"备份加密密钥无效（{BACKUP_KEY_ENV}）：{e}")


def _decrypt_fernets():
    """返回**解密**可用的全部候选 Fernet（主密钥优先，其后是历史/遗留密钥）。

    为什么需要多个：密钥位置在历史上迁移过（项目根 → 备份目录 → 用户配置目录），
    期间若被旧进程重新生成过，就会出现「同一批备份里不同包用了不同密钥」。
    解密时逐个尝试，任何一把能解开就恢复成功；加密仍只使用主密钥。
    """
    from cryptography.fernet import Fernet

    candidates: list[Path] = []
    env_key = os.environ.get(BACKUP_KEY_ENV)
    keys: list[str] = [env_key] if env_key else []
    if BACKUP_KEY_FILE.is_file():
        candidates.append(BACKUP_KEY_FILE)
    for legacy in _LEGACY_BACKUP_KEY_FILES:
        if legacy.is_file() and legacy not in candidates:
            candidates.append(legacy)
    # 隔离/归档的历史密钥（*.legacy*）也纳入尝试范围
    for legacy in _LEGACY_BACKUP_KEY_FILES:
        try:
            for archived in legacy.parent.glob(f"{legacy.name}*"):
                if archived.is_file() and archived not in candidates:
                    candidates.append(archived)
        except OSError:
            continue
    keys += [p.read_text(encoding="utf-8").strip() for p in candidates]

    seen, fernets = set(), []
    for raw in keys:
        if not raw or raw in seen:
            continue
        seen.add(raw)
        try:
            fernets.append(Fernet(raw.encode("utf-8")))
        except Exception:
            continue
    return fernets


def _encrypt_file(src: Path, dst: Path):
    """把 zip 明文加密为 .zip.enc（Fernet：AES128-CBC + HMAC）。"""
    dst.write_bytes(_fernet().encrypt(src.read_bytes()))


def _decrypt_file(src: Path, dst: Path):
    """把 .zip.enc 解密为临时 zip 明文（支持历史多密钥）。"""
    payload = src.read_bytes()
    errors = []
    for fernet in _decrypt_fernets():
        try:
            dst.write_bytes(fernet.decrypt(payload))
            return
        except Exception as e:  # 换下一把密钥继续尝试
            errors.append(type(e).__name__)
    raise ValueError(
        "备份包解密失败：已尝试全部已知密钥（主密钥与历史密钥）均不匹配。"
        "可能是密钥文件丢失，或该包由已不存在的密钥加密。"
        f"（尝试次数 {len(errors)}）"
    )


def is_backup_file_name(name: str) -> bool:
    """是否为受支持的备份包文件名（.zip 明文 / .zip.enc 加密）。

    统一入口：download / restore / delete / scan 全部走这里。
    改造前同一条规则在 5 处各写一遍（sysbackup 4 处 + views 1 处），
    实际就出现过「下载放行了 .zip.enc、恢复却只认 .zip」的不一致。
    """
    lowered = (name or "").lower()
    return lowered.endswith(".zip") or lowered.endswith(ENCRYPTED_SUFFIX)


def _is_encrypted(path: Path) -> bool:
    return str(path).lower().endswith(ENCRYPTED_SUFFIX)


def _mirror_backups(package: Path, task_id: str = "") -> list[dict]:
    """把刚生成的备份包复制到所有异地/镜像目录（审查 C2）。

    - 目标目录不存在则尝试创建（移动硬盘需先插上并存在盘符）；
    - 单个镜像失败只记警告并继续下一个，**不影响备份任务状态**；
    - 返回每个目录的结果，供任务详情与操作留痕展示。
    """
    results = []
    for raw in getattr(settings, "BACKUP_MIRROR_DIRS", []) or []:
        target_dir = Path(raw)
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / package.name
            shutil.copy2(package, target)
            # 校验大小一致，避免半截文件被当成有效备份
            if target.stat().st_size != package.stat().st_size:
                raise OSError("复制后大小不一致")
            results.append({"dir": str(target_dir), "ok": True, "path": str(target)})
            if task_id:
                _log(task_id, f"已复制异地副本：{target}")
        except OSError as exc:
            results.append({"dir": str(target_dir), "ok": False, "error": str(exc)})
            logger.warning("备份异地复制失败（已跳过）：%s -> %s", package.name, target_dir, exc_info=True)
            if task_id:
                _log(task_id, f"⚠️ 异地副本复制失败（{target_dir}）：{exc}")
    return results


def _rotate_backups(dest: Path) -> int:
    """按保留策略清理过期备份包，返回删除数量。

    规则（settings 可覆盖）：
    - 最近 ``BACKUP_RETAIN_DAILY``（默认 14）个包无条件保留；
    - 另外每周（ISO 周）各保留最新的 1 个，最多 ``BACKUP_RETAIN_WEEKLY``（默认 8）周。

    只看本系统命名的 ``backup_*{ENCRYPTED_SUFFIX}``：``safety_snapshots/`` 里的
    恢复前快照与用户自放的其它文件一律不动（破坏性最小化）。
    """
    retain_daily = max(1, int(getattr(settings, "BACKUP_RETAIN_DAILY", 14)))
    retain_weekly = max(0, int(getattr(settings, "BACKUP_RETAIN_WEEKLY", 8)))

    backups = [p for p in dest.glob(f"backup_*{ENCRYPTED_SUFFIX}") if p.is_file()]
    if len(backups) <= retain_daily:
        return 0
    backups.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    keep = set(backups[:retain_daily])
    # 每周保底：按 ISO 周取该周最新的一个
    weekly: dict = {}
    for path in backups:
        try:
            week_key = datetime.fromtimestamp(path.stat().st_mtime).isocalendar()[:2]
        except (OSError, ValueError):
            continue
        weekly.setdefault(week_key, path)
    keep |= set(list(weekly.values())[:retain_weekly] if retain_weekly else [])

    removed = 0
    for path in backups:
        if path in keep:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            logger.warning("备份轮转：删除 %s 失败（已跳过）", path, exc_info=True)
    return removed


def _human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} GB"


def _validate_dest_dir(dest_dir):
    """校验/创建备份目标目录，返回 Path。"""
    if not dest_dir:
        return Path(default_dir())
    p = Path(dest_dir).expanduser()
    if not p.is_absolute():
        raise ValueError("备份路径必须为绝对路径")
    ensure_path_allowed(p)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ValueError(f"备份目录不可创建：{e}")
    probe = p / ".backup_probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError:
        raise ValueError("备份目录不可写，请更换路径")
    return p


def _iter_storage_files(prefix=""):
    """递归遍历 default_storage，产出相对路径（正斜杠）。

    MEDIA_ROOT 尚未创建时（全新部署、CI 空库、刚清理过附件目录）listdir 会抛
    FileNotFoundError；备份不应因此整体失败，按「无附件」处理。
    """
    try:
        dirs, files = default_storage.listdir(prefix or "")
    except (FileNotFoundError, NotADirectoryError):
        return
    for f in files:
        yield (prefix + f) if prefix else f
    for d in dirs:
        if d in MEDIA_EXCLUDE_DIRS:
            continue
        child = f"{prefix}{d}/" if prefix else f"{d}/"
        yield from _iter_storage_files(child)


def _storage_source(rel):
    """返回 (zip写入源, 大小或None)：本地 storage 返回真实路径以便零拷贝打包，云 storage 返回文件流。"""
    if hasattr(default_storage, "path"):
        try:
            full = default_storage.path(rel)
            return full, os.path.getsize(full)
        except (NotImplementedError, OSError):
            pass
    return default_storage.open(rel), None


def _collect_module_stats():
    """统计各业务 app 的模型与记录数（用于元信息与恢复校验）。"""
    stats = []
    total_rows = 0
    for app_conf in apps.get_app_configs():
        if app_conf.name in ("django.contrib.staticfiles", "corsheaders", "rest_framework"):
            continue
        models_info = []
        for model in app_conf.get_models():
            try:
                cnt = model._default_manager.count()
            except Exception:
                cnt = None  # 无迁移表等极端情况不阻断备份
            models_info.append({"model": model.__name__, "rows": cnt})
            if isinstance(cnt, int):
                total_rows += cnt
        if models_info:
            stats.append({"app": app_conf.label, "models": models_info})
    return stats, total_rows


def _write_meta(path, *, backup_name, operator, media_files, media_total, db_tables, db_rows):
    meta = {
        "schema_version": SCHEMA_VERSION,
        "created_at": timezone.localtime().isoformat(),
        "created_by": operator,
        "product": "每日工作记录管理网站",
        "django_version": _django_version(),
        "database": {"engine": "sqlite3", "tables": db_tables, "rows": db_rows},
        "modules": _module_list(),
        "model_stats": _collect_module_stats()[0],
        "media": {
            "file_count": len(media_files),
            "total_size": media_total,
            "files": media_files,
        },
        "backup_file": backup_name,
    }
    Path(path).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def _django_version():
    import django

    return django.get_version()


def _module_list():
    """备份覆盖的业务模块列表（app label + 中文名尽量展示）。"""
    labels = []
    for app_conf in apps.get_app_configs():
        if app_conf.label in ("contenttypes", "auth", "sessions", "admin", "messages",
                              "staticfiles", "corsheaders", "rest_framework"):
            continue
        labels.append(app_conf.label)
    return labels


# ---------- 备份任务 ----------

def start_backup(dest_dir=None, operator=""):
    """启动全量备份异步任务，返回 task_id。目录非法时抛 ValueError。"""
    dest = _validate_dest_dir(dest_dir)
    if not _acquire_slot():
        raise RuntimeError("已有备份/恢复任务正在运行，请稍后再试")
    task_id = _new_task("backup", detail=f"目标目录：{dest}")
    threading.Thread(target=_run_backup, args=(task_id, dest, operator), daemon=True).start()
    return task_id


def _acquire_slot():
    with _STATE_LOCK:
        if _RUN_SLOT[0]:
            return False
        _RUN_SLOT[0] = True
        return True


def _release_slot():
    with _STATE_LOCK:
        _RUN_SLOT[0] = False


def _run_backup(task_id, dest: Path, operator):
    tmp_dir = Path(settings.BASE_DIR) / "backups" / f".tmp_{task_id[:8]}"
    backup_name = ""
    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
        backup_name = f"backup_{timezone.localtime().strftime('%Y%m%d_%H%M%S')}{ENCRYPTED_SUFFIX}"
        final_path = dest / backup_name

        # 1) 数据库在线快照（表结构 + 全部数据）
        _progress(task_id, 5, "备份数据库")
        _log(task_id, "开始数据库在线快照（SQLite backup API）")
        db_tmp = tmp_dir / DB_ENTRY
        src = sqlite3.connect(str(settings.DATABASES["default"]["NAME"]))
        dst = sqlite3.connect(str(db_tmp))
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        db_tables = sqlite3.connect(str(db_tmp)).execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()[0]
        _log(task_id, f"数据库快照完成：{db_tables} 张表，{_human_size(db_tmp.stat().st_size)}")

        # 2) 生成 SQL 文本副本（标准库 iterdump，仅导出不执行）
        _progress(task_id, 15, "生成 SQL 导出文件")
        sql_tmp = tmp_dir / SQL_ENTRY
        con = sqlite3.connect(str(db_tmp))
        with open(sql_tmp, "w", encoding="utf-8", newline="\n") as fh:
            for line in con.iterdump():
                fh.write(line + "\n")
        con.close()
        _log(task_id, f"SQL 导出完成：{_human_size(sql_tmp.stat().st_size)}")

        # 3) 收集附件清单
        _progress(task_id, 25, "收集附件清单")
        media_files = list(_iter_storage_files())
        _log(task_id, f"共发现附件 {len(media_files)} 个")

        # 4) 打包 zip（DB + SQL + media + meta）
        _progress(task_id, 30, "打包备份文件")
        tmp_zip = tmp_dir / "package.zip"
        media_total = 0
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            zf.write(db_tmp, DB_ENTRY)
            zf.write(sql_tmp, SQL_ENTRY)

            for i, rel in enumerate(media_files):
                source, size = _storage_source(rel)
                try:
                    if size is not None:
                        zf.write(source, MEDIA_PREFIX + rel)
                    else:
                        with zf.open(MEDIA_PREFIX + rel, "w") as target:
                            while True:
                                chunk = source.read(1024 * 256)
                                if not chunk:
                                    break
                                target.write(chunk)
                        size = source.seek(0, 2)
                finally:
                    if hasattr(source, "close"):
                        source.close()
                media_total += size or 0
                if i % 50 == 0 or i == len(media_files) - 1:
                    _progress(task_id, 30 + int(60 * (i + 1) / max(1, len(media_files))), "打包附件文件")

            _progress(task_id, 92, "写入元信息")
            meta = _write_meta(
                tmp_dir / META_ENTRY,
                backup_name=backup_name,
                operator=operator,
                media_files=media_files,
                media_total=media_total,
                db_tables=db_tables,
                db_rows=sum(
                    m["rows"] or 0
                    for app_ in _collect_module_stats()[0]
                    for m in app_["models"]
                ),
            )
            zf.write(tmp_dir / META_ENTRY, META_ENTRY)

        # 5) 加密后原子落盘到目标路径（明文 zip 只存在于临时目录，结束即清理）
        _progress(task_id, 97, "加密并写入目标路径")
        tmp_enc = tmp_dir / "package.zip.enc"
        _encrypt_file(tmp_zip, tmp_enc)
        os.replace(str(tmp_enc), str(final_path))
        _log(task_id, f"备份包已生成：{final_path}（{_human_size(final_path.stat().st_size)}）")

        # 5.1) 保留策略轮转（审查 H7）：每日全量包永不删除会把单盘写满，
        #      盘满后 SQLite 写入与后续备份会一起失败。轮转失败只记警告，
        #      绝不影响本次备份的成功状态。
        try:
            removed = _rotate_backups(dest)
            if removed:
                _log(task_id, f"保留策略：清理 {removed} 个过期备份包"
                              f"（保留最近 {settings.BACKUP_RETAIN_DAILY} 天 + 每周 1 份 × "
                              f"{settings.BACKUP_RETAIN_WEEKLY} 周）")
        except Exception:  # noqa: BLE001 — 轮转属附属动作，不能让备份失败
            logger.exception("备份轮转失败（已忽略，备份包仍有效）：%s", dest)

        # 5.2) 异地副本（审查 C2）：备份与数据库同盘时，磁盘故障会双失。
        mirror_results = _mirror_backups(final_path, task_id)

        # 6) 操作留痕（先写记录，再标记任务完成，保证轮询到任务结束时记录已存在）
        mirror_note = ""
        if mirror_results:
            ok_n = sum(1 for m in mirror_results if m["ok"])
            mirror_note = f"；异地副本 {ok_n}/{len(mirror_results)} 成功"
        _write_oplog(operator, "备份", backup_name,
                     f"全量备份至 {final_path}：{db_tables} 张表 / {len(media_files)} 个附件{mirror_note}")
        _log_operation(
            "backup", operator, backup_name, str(final_path), True,
            f"全量备份至 {final_path}（{_human_size(final_path.stat().st_size)}）："
            f"数据库 {db_tables} 张表，附件 {len(media_files)} 个",
        )

        _update(
            task_id,
            status="success",
            percent=100,
            stage="备份完成",
            message=f"备份完成：{backup_name}（{_human_size(final_path.stat().st_size)}，"
                    f"{db_tables} 张表，{len(media_files)} 个附件）",
            result={
                "path": str(final_path),
                "name": backup_name,
                "mirrors": mirror_results,
                "size": final_path.stat().st_size,
                "db_tables": db_tables,
                "media_files": len(media_files),
            },
        )
    except Exception as e:
        _log(task_id, "备份失败：" + str(e))
        _log_operation("backup", operator, backup_name, "", False, f"备份失败：{e}")
        # error 字段只回传简短信息给前端，完整 traceback 仅写入日志，避免内部细节泄露
        logger.exception("备份任务 %s 失败", task_id)
        _update(task_id, status="failed", stage="备份失败", error=str(e),
                message=f"备份失败：{e}")
    finally:
        set_maintenance_mode(False)
        _release_slot()
        _cleanup_tmp(tmp_dir)


def _cleanup_tmp(tmp_dir: Path):
    try:
        if tmp_dir.exists():
            for p in sorted(tmp_dir.rglob("*"), reverse=True):
                p.unlink(missing_ok=True) if p.is_file() else p.rmdir()
            tmp_dir.rmdir()
    except OSError:
        pass


def _write_oplog(operator, op_type, target, summary):
    """写操作留痕（复用 records.audit 的统一写入点，失败不影响主流程）。"""
    try:
        from records.audit import write_operation

        write_operation(
            operator=operator or "-",
            module_type="系统管理",
            operation_type=op_type,
            object_identifier=target,
            change_summary=summary,
        )
    except Exception:
        # 留痕失败不影响备份/恢复主流程，但必须留痕以便事后审计
        logger.exception("写入操作留痕（records.OperationLog）失败：op_type=%s target=%s", op_type, target)


# ---------- 备份/恢复操作记录（独立留痕，含失败记录） ----------

_OPLOG_LOCK = threading.Lock()
OPLOG_FILE = Path(settings.BASE_DIR) / "backups" / "operation_logs.jsonl"


def _ensure_log_table():
    """确保操作记录表存在（恢复旧版本备份包后该表可能随库回滚而消失）。"""
    from django.db import connection

    from .models import BackupOperationLog

    table = BackupOperationLog._meta.db_table
    if table not in connection.introspection.table_names(connection.cursor()):
        with connection.schema_editor() as editor:
            editor.create_model(BackupOperationLog)


def _log_operation(op_type, operator, file_name="", file_path="",
                   success=True, detail=""):
    """写入备份/恢复操作记录：数据库表 + JSONL 文件双写。

    恢复操作会将数据库整体回退，表内记录可能丢失；
    JSONL 文件不受恢复影响，恢复完成后由 sync_logs_from_file() 回填。
    任何写入失败均不影响备份/恢复主流程。
    """
    entry = {
        "id": uuid.uuid4().hex,
        "op_type": op_type,
        "operator": operator or "-",
        "file_name": file_name or "",
        "file_path": str(file_path or ""),
        "success": bool(success),
        "detail": detail or "",
        "created_at": timezone.localtime().isoformat(),
    }
    try:
        _ensure_log_table()
        from .models import BackupOperationLog

        BackupOperationLog.objects.create(
            id=entry["id"], op_type=op_type, operator=entry["operator"],
            file_name=entry["file_name"], file_path=entry["file_path"],
            success=entry["success"], detail=entry["detail"],
            created_at=datetime.fromisoformat(entry["created_at"]),
        )
    except Exception:
        # 表写入失败不影响主流程（JSONL 仍会落盘，恢复后可回填），但需留痕
        logger.exception("备份/恢复操作记录写库失败：op_type=%s file=%s", op_type, file_name)
    try:
        OPLOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _OPLOG_LOCK:
            with open(OPLOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def sync_logs_from_file():
    """恢复数据库后，将 JSONL 文件中的操作记录回填表内（补回被回滚的记录）。

    幂等：按记录 id 去重，重复调用无副作用。返回回填条数。
    """
    from .models import BackupOperationLog

    if not OPLOG_FILE.is_file():
        return 0
    try:
        _ensure_log_table()
    except Exception:
        return 0
    try:
        with _OPLOG_LOCK:
            lines = OPLOG_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    restored = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
            if BackupOperationLog.objects.filter(id=e.get("id", "")).exists():
                continue
            created = datetime.fromisoformat(e.get("created_at"))
            if timezone.is_naive(created):
                created = timezone.make_aware(created, timezone.get_current_timezone())
            BackupOperationLog.objects.create(
                id=e["id"], op_type=e.get("op_type", "backup"),
                operator=e.get("operator", "-"), file_name=e.get("file_name", ""),
                file_path=e.get("file_path", ""), success=bool(e.get("success", True)),
                detail=e.get("detail", ""), created_at=created,
            )
            restored += 1
        except Exception:
            # 单行回填失败不阻断整体回填，但需留痕定位坏数据
            logger.exception("回填操作记录失败，跳过该行：%s", line[:120])
            continue
    return restored


# ---------- 恢复 ----------

def start_restore(zip_path, operator=""):
    """启动全量恢复异步任务，返回 task_id。路径非法时抛 ValueError。"""
    p = Path(zip_path)
    if not p.is_absolute() or not p.is_file() or not is_backup_file_name(p.name):
        raise ValueError("备份包路径无效")
    ensure_path_allowed(p)
    if not _acquire_slot():
        raise RuntimeError("已有备份/恢复任务正在运行，请稍后再试")
    task_id = _new_task("restore", detail=f"备份包：{p}")
    threading.Thread(target=_run_restore, args=(task_id, p, operator), daemon=True).start()
    return task_id


def _inspect_backup(zip_path: Path):
    """解析并校验备份包，返回 (meta, missing_files)。"""
    errors = []
    meta = None
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        if META_ENTRY not in names:
            errors.append("缺少 meta.json 元信息文件")
        if DB_ENTRY not in names:
            errors.append("缺少数据库快照 db.sqlite3")
        if not errors:
            meta = json.loads(zf.read(META_ENTRY).decode("utf-8"))
            if meta.get("schema_version") != SCHEMA_VERSION:
                errors.append(
                    f"备份包结构版本不兼容（包 v{meta.get('schema_version')}，当前 v{SCHEMA_VERSION}）"
                )
            pkg_ver = str(meta.get("django_version", ""))
            pkg_major = pkg_ver.split(".")[0]
            cur_major = _django_version().split(".")[0]
            # 跨主版本：旧包 → 新版本允许（本项目 4.2 升级 5.2 后，升级前的历史备份仍需可恢复；
            # 数据库迁移记录与表结构一致，migrate --check 已验证无待补齐项）。
            # 新包 → 旧版本禁止：缺少逆向迁移，降级恢复会得到无法识别的 schema。
            if pkg_major.isdigit() and cur_major.isdigit() and int(pkg_major) > int(cur_major):
                errors.append(
                    f"备份包 Django 版本高于当前版本，无法降级恢复"
                    f"（备份时 {pkg_ver}，当前 {_django_version()}）"
                )
            elif pkg_major and pkg_major != cur_major:
                logger.warning(
                    "恢复旧版本备份包：备份时 Django %s，当前 %s，按当前版本向前兼容",
                    pkg_ver, _django_version(),
                )
            manifest = meta.get("media", {}).get("files", [])
            missing = [f for f in manifest if (MEDIA_PREFIX + f) not in names]
            if missing:
                errors.append(f"附件清单缺失 {len(missing)} 个文件，如：{missing[:5]}")

            # 所有 zip 条目名路径围栏预检——任何越界条目直接判包不合法
            suspicious_paths = []
            for entry in names:
                if not entry.startswith(MEDIA_PREFIX) or entry.endswith("/"):
                    continue
                rel = entry[len(MEDIA_PREFIX):]
                if _safe_rel(rel) is None:
                    suspicious_paths.append(entry)
            if suspicious_paths:
                errors.append(
                    f"备份包中存在 {len(suspicious_paths)} 个可疑条目（可能包含路径穿越），"
                    f"示例：{suspicious_paths[:3]}"
                )
    return meta, errors


MAINTENANCE_CACHE_KEY = "sysbackup_maintenance_mode"


def set_maintenance_mode(on: bool) -> None:
    """恢复期间置维护标志：中间件据此拒绝非超管的写请求（审查 S2/B2）。

    用 DatabaseCache（settings 已配置）而非进程内字典：恢复在线程里跑、
    runserver 多线程共享，且将来若换进程也能读到同一状态。
    """
    from django.core.cache import cache

    try:
        if on:
            cache.set(MAINTENANCE_CACHE_KEY, True, timeout=2 * 3600)
        else:
            cache.delete(MAINTENANCE_CACHE_KEY)
    except Exception:  # 缓存不可用不能阻断恢复/备份主流程
        logger.warning("维护模式标志写入失败（已忽略）", exc_info=True)


def in_maintenance_mode() -> bool:
    from django.core.cache import cache

    try:
        return bool(cache.get(MAINTENANCE_CACHE_KEY))
    except Exception:
        return False


def _restore_db_from(src_db: Path) -> None:
    """用在线备份 API 把 src_db 灌回活动库并关闭所有连接（失败回滚用）。"""
    from django.db import connections

    src = sqlite3.connect(str(src_db))
    dst = sqlite3.connect(str(settings.DATABASES["default"]["NAME"]))
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()
    connections.close_all()


def _run_restore(task_id, zip_path: Path, operator):
    tmp_dir = Path(settings.BASE_DIR) / "backups" / f".tmp_{task_id[:8]}"
    # 维护模式：整个恢复期间拒绝非超管写入，避免「库已换成备份、附件还没恢复」
    # 的半状态下继续产生新数据（审查 S2/B2）。finally 里一定会解除。
    set_maintenance_mode(True)
    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
        package_zip = zip_path
        if _is_encrypted(zip_path):
            _progress(task_id, 3, "解密备份包")
            package_zip = tmp_dir / "package.zip"
            _decrypt_file(zip_path, package_zip)
            _log(task_id, "备份包已解密，开始结构校验")
        meta, errors = _inspect_backup(package_zip)
        if errors:
            raise ValueError("备份包校验未通过：\n" + "\n".join(errors))

        meta_created = str(meta.get("created_at", ""))[:19].replace("T", " ")
        _log(task_id, f"备份包校验通过：{zip_path.name}（备份于 {meta_created}，操作人 {meta.get('created_by')}）")
        _log(task_id, f"Django {meta.get('django_version')}，数据库 {meta['database']['tables']} 张表 / "
                      f"{meta['database']['rows']} 条记录，附件 {meta['media']['file_count']} 个")
        if meta.get("django_version") != _django_version():
            pkg_major = str(meta.get("django_version", "")).split(".")[0]
            note = "已确认兼容（旧版本向前兼容）" if pkg_major == _django_version().split(".")[0] \
                else "旧主版本包，按当前版本读取"
            _log(task_id, f"⚠️ Django 版本不同（当前 {_django_version()}），{note}")

        # 1) 恢复前对当前库做安全快照，失败时可手动回退
        _progress(task_id, 8, "备份当前数据（安全快照）")
        safety_dir = Path(settings.BASE_DIR) / "backups" / "safety_snapshots"
        safety_dir.mkdir(parents=True, exist_ok=True)
        safety_db = safety_dir / f"before_restore_{timezone.localtime().strftime('%Y%m%d_%H%M%S')}.sqlite3"
        src = sqlite3.connect(str(settings.DATABASES["default"]["NAME"]))
        dst = sqlite3.connect(str(safety_db))
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        _log(task_id, f"当前数据安全快照：{safety_db.name}")

        # 2~3) 换库 + 恢复附件：任一步失败都必须回滚到安全快照，
        # 否则会停在「库已换、附件只解压了一半」的半状态（审查 S2/B2）。
        db_replaced = False
        try:
            _progress(task_id, 20, "恢复数据库")
            db_tmp = tmp_dir / DB_ENTRY
            with zipfile.ZipFile(package_zip) as zf:
                zf.extract(DB_ENTRY, tmp_dir)
            _restore_db_from(db_tmp)
            db_replaced = True
            _log(task_id, "数据库恢复完成")

            # 3) 恢复附件：解压覆盖 + 清理备份时点之后新增的文件
            manifest = set(meta["media"]["files"])
            with zipfile.ZipFile(package_zip) as zf:
                media_entries = [n for n in zf.namelist()
                                 if n.startswith(MEDIA_PREFIX) and not n.endswith("/")]
                total = len(media_entries)
                for i, entry in enumerate(media_entries):
                    rel = entry[len(MEDIA_PREFIX):]
                    _write_storage_file(rel, zf.open(entry))
                    if i % 50 == 0 or i == total - 1:
                        _progress(task_id, 30 + int(55 * (i + 1) / max(1, total)), "恢复附件文件")

            _progress(task_id, 88, "同步附件差异")
            removed = 0
            for rel in list(_iter_storage_files()):
                if rel not in manifest:
                    default_storage.delete(rel)
                    removed += 1
            _log(task_id, f"附件恢复完成：写入 {len(media_entries)} 个，清理备份后新增文件 {removed} 个")
        except Exception as inner_exc:
            if db_replaced:
                _log(task_id, f"恢复中途失败（{inner_exc}），正在回滚到恢复前的安全快照…")
                try:
                    _restore_db_from(safety_db)
                    _log(task_id, f"已自动回滚到安全快照 {safety_db.name}；附件可能处于混合状态，请人工核对")
                except Exception:
                    logger.exception("自动回滚失败，需人工用 %s 恢复", safety_db)
                    _log(task_id, f"⚠️ 自动回滚失败！请人工用安全快照恢复：{safety_db}")
            raise

        # 4) 操作留痕（先回填被数据库回退丢失的操作记录，再写入本次恢复记录）
        _progress(task_id, 95, "写入操作留痕")
        synced = sync_logs_from_file()
        if synced:
            _log(task_id, f"已从留痕文件回填操作记录 {synced} 条")
        _write_oplog(operator, "恢复", zip_path.name,
                     f"全量恢复自 {zip_path}（备份于 {meta_created}），附件 {len(media_entries)} 个，清理新增 {removed} 个")
        _log_operation(
            "restore", operator, zip_path.name, str(zip_path), True,
            f"全量恢复自 {zip_path}（备份于 {meta_created}）：数据库 {meta['database']['tables']} 张表，"
            f"附件 {len(media_entries)} 个，清理新增 {removed} 个，安全快照 {safety_db.name}",
        )

        _update(
            task_id,
            status="success",
            percent=100,
            stage="恢复完成",
            message=f"恢复完成：数据库 {meta['database']['tables']} 张表，附件 {len(media_entries)} 个"
                    f"（安全快照：{safety_dir.name}/{safety_db.name}）",
            result={"removed_files": removed, "safety_snapshot": str(safety_db)},
        )
    except Exception as e:
        _log(task_id, "恢复失败：" + str(e))
        logger.exception("恢复任务 %s 失败", task_id)
        # 先回填被数据库回退丢失的操作记录并写入失败留痕，再标记任务完成
        sync_logs_from_file()
        _log_operation("restore", operator, zip_path.name, str(zip_path), False,
                       f"恢复失败：{e}")
        _update(task_id, status="failed", stage="恢复失败", error=str(e),
                message=f"恢复失败：{e}")
    finally:
        _release_slot()
        _cleanup_tmp(tmp_dir)


def _safe_rel(rel: str) -> str | None:
    """对 zip 条目截取得到的相对路径做围栏校验。

    返回规范化后的安全相对路径；越界 / 绝对路径 / 非法输入返回 None。

    使用栈模拟路径遍历：遇到 '.' 跳过、遇到 '..' 弹出上一段，
    若弹出时栈已空说明路径在 zip 内已越出 media 根目录。
    """
    if not rel:
        return None
    # 拒绝以 / 或 \ 开头的绝对路径
    if rel.startswith(("/", "\\")):
        return None
    # 统一分隔符为 /
    normalized = rel.replace("\\", "/")
    stack: list[str] = []
    for seg in normalized.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if not stack:
                # 弹出时栈已空——说明从一开始就在试图穿越根目录
                return None
            stack.pop()
        else:
            stack.append(seg)
    if not stack:
        return None
    return "/".join(stack)


def _write_storage_file(rel, src_fh):
    """将 zip 内文件对象写入 storage（本地直写路径；云存储走 save 接口）。

    rel 来自 zip 条目名，必须先通过 _safe_rel 路径围栏校验。
    """
    safe = _safe_rel(rel)
    if safe is None:
        raise SuspiciousOperation(f"非法备份条目路径：{rel!r}（可能包含路径穿越或空片段）")
    rel = safe

    if hasattr(default_storage, "path"):
        try:
            full = Path(default_storage.path(rel))
            full.parent.mkdir(parents=True, exist_ok=True)
            with open(full, "wb") as dst:
                dst.write(src_fh.read())
            return
        except (NotImplementedError, OSError):
            pass
    from django.core.files.base import ContentFile

    default_storage.delete(rel)
    default_storage.save(rel, ContentFile(src_fh.read()))


# ---------- 备份包管理 ----------

def scan_backups(dir_path=None):
    """扫描目录下的备份包列表（按备份时间倒序）。目录必须在白名单内。"""
    base = Path(dir_path) if dir_path else Path(default_dir())
    ensure_path_allowed(base)
    if not base.is_dir():
        return [], str(base)
    items = []
    for f in base.iterdir():
        if not f.is_file():
            continue
        if not is_backup_file_name(f.name):
            continue
        info = _read_backup_summary(f)
        if info:
            items.append(info)
    items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return items, str(base)


def _read_backup_summary(f: Path):
    try:
        summary_zip = f
        tmp_name = None
        if _is_encrypted(f):
            # 加密包无明文索引，解密到临时文件读 meta 后立即删除
            fd, tmp_name = tempfile.mkstemp(suffix=".zip")
            os.close(fd)
            summary_zip = Path(tmp_name)
            _decrypt_file(f, summary_zip)
        with zipfile.ZipFile(summary_zip) as zf:
            if META_ENTRY not in zf.namelist():
                return None
            meta = json.loads(zf.read(META_ENTRY).decode("utf-8"))
        stat = f.stat()
        return {
            "name": f.name,
            "path": str(f),
            "size": stat.st_size,
            "size_text": _human_size(stat.st_size),
            "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "created_at": str(meta.get("created_at", ""))[:19].replace("T", " "),
            "created_by": meta.get("created_by", ""),
            "django_version": meta.get("django_version", ""),
            "db_tables": meta.get("database", {}).get("tables"),
            "db_rows": meta.get("database", {}).get("rows"),
            "media_files": meta.get("media", {}).get("file_count"),
            "schema_version": meta.get("schema_version"),
            "encrypted": _is_encrypted(f),
            "readable": True,
        }
    except Exception as exc:
        # 读不出 meta（密钥缺失/包损坏）不能静默丢弃 —— 否则用户会看到"备份凭空消失"，
        # 反而更慌。这里返回降级信息，页面标记「无法解密」并只允许删除。
        logger.warning("备份包无法读取（%s）：%s", f.name, exc)
        try:
            stat = f.stat()
        except OSError:
            return None
        mtime_text = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        return {
            "name": f.name,
            "path": str(f),
            "size": stat.st_size,
            "size_text": _human_size(stat.st_size),
            "mtime": mtime_text,
            "created_at": mtime_text,
            "created_by": "（无法读取）",
            "django_version": "",
            "db_tables": "—",
            "db_rows": "—",
            "media_files": None,
            "schema_version": None,
            "encrypted": _is_encrypted(f),
            "readable": False,
            "unreadable_reason": type(exc).__name__,
        }
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def delete_backup(zip_path):
    p = Path(zip_path)
    if not p.is_absolute() or not p.is_file() or not is_backup_file_name(p.name):
        raise ValueError("备份包路径无效")
    ensure_path_allowed(p)
    p.unlink()
    return p.name


def wait_task(task_id, timeout=300):
    """仅供测试/运维：阻塞等待任务结束。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = _task_snapshot(task_id)
        if snap and snap["status"] != "running":
            return snap
        time.sleep(0.5)
    return _task_snapshot(task_id)
