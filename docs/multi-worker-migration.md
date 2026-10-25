# 多进程/多实例改造清单（C4）

> 现状：本项目**明确依赖单进程**运行（`work_record/settings.py` 的「部署形态」注释、
> `README` 部署章节）。本文只回答一个问题：**如果将来必须换多 worker（gunicorn / waitress
> 多进程 / 多台机器），需要先改哪些代码**。在完成下面每一项之前，多 worker 部署会出现
> 静默错误（不是报错，而是行为不一致），因此不要"先上多进程再说"。

## 一、当前依赖单进程的 5 处实现

| # | 位置 | 依赖形态 | 多进程后会发生什么 |
|---|---|---|---|
| 1 | `sysbackup/services.py` `_TASKS` / `_RUN_SLOT` | 进程内字典 + 进程内 `threading.Lock` 互斥 | 进程 A 的备份任务在 B 查不到（进度恒 0/丢失）；互斥失效 → 两个进程可同时备份或同时恢复，SQLite 文件被两个恢复流程交替写入 |
| 2 | `notifications/scheduler.py` 心跳 | `logs/.hb_worker` / `logs/.hb_scheduler` 文件时间戳 | 多进程同时写心跳文件，健康页无法判断"是哪个进程活着"；若每个 worker 都起调度器，任务被重复执行 |
| 3 | `notifications/views.py` 定时任务手动执行 | 直接调用 `scheduler.run_task`（进程内线程） | 从 A 触发、在 B 看不到状态；与自动调度并发时依赖 `claim_for_send` 的原子领取兜底（DB 层有约束，不会双发，但人工触发会静默无效） |
| 4 | `work_record/export_guard.py` 信号量 | 进程内 `BoundedSemaphore` | 每个进程各自限 2，实际并发 = 2 × 进程数，内存保护失效（**换多进程时必须同时把上限调小**） |
| 5 | `records/audit.py` `log_operation` | 直接写 DB（跨进程安全） | 这项本身没问题；风险在"谁写的"——多进程后需确认每进程都有正确时间/操作人上下文 |

## 二、改造顺序（建议）

1. **任务状态外置**：把 `sysbackup` 的 `_TASKS`/互斥完全改为 DB 表（`BackupTask` 已存在，
   目前只是"内存为主、DB 为辅"）。互斥改为 `SELECT ... FOR UPDATE` 或
   "插入唯一键 `running` 行"的方式。
2. **调度器独立化**（已完成一半）：`run_scheduler` 已是独立进程；多 worker 时必须保证
   **仅调度进程**执行定时任务（已有 `NOTIFICATIONS_SCHEDULER_INPROCESS=False` 默认值，
   多进程下也不要打开），并让心跳带 PID/进程标识。
3. **导出闸门换分布式计数**：用缓存（`django_cache_table` 或 Redis）做原子计数，
   或用"文件锁 + 计数"的轻量方案。
4. **恢复期维护模式**：已用 DatabaseCache，天然跨进程；但要确认所有 worker 都读同一个缓存表。
5. **备份/恢复改为独立命令**：`manage.py backup_now` / `manage.py restore_backup <pkg>`，
   web 只负责排队与展示 —— 这一步做完，多进程才真正安全（长任务不再跑在请求进程里）。

## 三、必须同时复核的假设

- `work_record/settings.py` 中 `CACHES` 用 DatabaseCache：多进程可用，但**每次缓存读写都是一次
  SQLite 写事务**，多进程并发会显著放大锁竞争 → 上多进程前应先评估换 Redis。
- `accounts/security.py` 登录锁定依赖 DatabaseCache：跨进程语义正确，但同样有写放大问题。
- `sysbackup/tests_views.py`、`work_record/tests_rbac_coverage.py` 等测试用例里对"单进程"
  的假设（如直接读 `_TASKS`）需要同步改造。
- `README` 里"两进程形态"的描述、`start*.sh/bat` 的启动顺序都要一起改。

## 四、结论

**单机 + SQLite + 十几人内网**的现状下，不需要多进程：瓶颈在 SQLite 单写者与磁盘，
不在 Python 进程数。若并发真的上升，优先考虑：
1. 换 PostgreSQL（`DATABASES` 一行配置 + 迁移）；
2. 备份/恢复移出 web 进程；
3. 再谈多 worker。

在此之前保持 `--noreload` + 单进程，是当前架构下**正确**的选择，不是妥协。
