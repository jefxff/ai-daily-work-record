"""系统健康探测（只读）。

把原先散落在 ai_api.views.health_check 里的三项探测（数据库 / 未应用迁移 /
日志目录可写）迁移至此，供 API 探针与「系统健康面板」共用，避免同一逻辑两处
实现产生漂移。

探测函数内部绝不抛异常：逐项 try/except 兜底并记录日志，返回结构化结果。

性能提示：MigrationExecutor 构造需扫描 70+ 迁移文件，Windows 上 ~140ms。
probe_health 结果 60 秒 TTL 缓存，过期才重算。
"""
import logging
import os
import time

from django.conf import settings
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

logger = logging.getLogger(__name__)

# probe_health 缓存：60 秒过期（数据库连接/迁移状态/日志目录都是秒级以上才变）
_CACHE_TTL = 60
_CACHED_RESULT = None   # (checks, healthy) 元组
_CACHED_AT = 0          # 时间戳


def probe_health():
    """探测数据库 / 未应用迁移 / 日志目录，返回 (checks, healthy)。

    checks 结构（键名与取值语义保持与改造前完全一致）：
      database   : 'ok' / 'error: <ExceptionName>'
      migrations : 'ok' / 'pending: N' / 'unknown'
      log_dir    : 'ok' / 'error'

    healthy = checks['database'] == 'ok' and checks['log_dir'] == 'ok'。

    60 秒内直接返回上次结果，过期才重算（MigrationExecutor 构造是主要耗时点）。
    """
    global _CACHED_RESULT, _CACHED_AT
    now = time.monotonic()
    if _CACHED_RESULT is not None and (now - _CACHED_AT) < _CACHE_TTL:
        return _CACHED_RESULT

    checks = {}

    # 1) 数据库连接（SELECT 1）
    try:
        with connection.cursor() as cur:
            cur.execute('SELECT 1')
            cur.fetchone()
        checks['database'] = 'ok'
    except Exception as exc:  # 探针本身不能抛异常
        logger.exception('健康检查：数据库不可用')
        checks['database'] = f'error: {exc.__class__.__name__}'

    # 2) 是否有未应用的迁移
    try:
        executor = MigrationExecutor(connection)
        pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
        checks['migrations'] = 'ok' if not pending else f'pending: {len(pending)}'
    except Exception:
        logger.exception('健康检查：迁移状态不可读')
        checks['migrations'] = 'unknown'

    # 3) 日志目录可写（日志写不进去时排障会完全失明）
    try:
        log_dir = os.path.join(str(settings.BASE_DIR), 'logs')
        probe = os.path.join(log_dir, '.health_probe')
        with open(probe, 'w', encoding='utf-8') as fh:
            fh.write('ok')
        os.remove(probe)
        checks['log_dir'] = 'ok'
    except Exception:
        logger.exception('健康检查：日志目录不可写')
        checks['log_dir'] = 'error'

    healthy = checks['database'] == 'ok' and checks['log_dir'] == 'ok'
    _CACHED_RESULT = (checks, healthy)
    _CACHED_AT = now
    return _CACHED_RESULT
