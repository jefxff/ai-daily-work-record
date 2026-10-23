# 变更日志

本文件记录对外可见的功能、部署形态与依赖变更。格式参考 Keep a Changelog。

## [2.1.0] - 2026-10-23

评审复核报告（`.trae/documents/评审复核报告-20261023.md`）整改。全部改动保持 URL、
URL 名称、模板引用与业务数据不变。

### 变更（结构）

- `records/views.py` 1660 行 → 约 800 行：`home` / `search` / `dashboard` 分别迁到
  `views_home.py` / `views_search.py` / `views_dashboard.py`，原文件保留门面 re-export。
- `ai_api/views.py` 1175 行 → 门面：ViewSet 迁到 `viewsets.py`，AI 端点迁到 `views_ai.py`，
  `urls.py` 与导入路径零改动。
- `dashboard()` 接入 `records/stats.py`：核减金额改为 SQL 聚合，核价核减用
  `Greatest(Coalesce(...))` 下推，`DASHBOARD_MODULE_KEYS`/`dashboard_summary` 统一口径。
- 新增 `work_record/http_utils.py`，统一 `is_ajax` / `per_page` / `parse_date`
  （替换 `records`、`samples`、`notifications`、`accounts`、`archive`、`knowledge_base`
  共 17 处重复实现）。

### 变更（可靠性）

- `sysbackup` 任务状态由进程内字典改为「内存缓存 + 落库」（新增 `BackupTask` 表与
  `0002_backuptask` 迁移）：服务重启后进度/日志/结果仍可读，僵死超过 30 分钟的任务可回收。
- 操作留痕保留期可配置（`OPERATION_LOG_RETENTION_DAYS`，默认 730 天），新增
  `manage.py cleanup_operation_log`（支持 `--dry-run`），并注册为可调度的定时任务。

### 变更（安全）

- 4 个 RBAC 验收账号的口令已随机化（旧口令失效），新增
  `manage.py rotate_default_passwords` 支持一次性轮换与 `--force`。

### 变更（CI 门禁）

- ruff 规则由 `pyproject.toml` 统一（`E9/F63/F7/F82/F811/F821/F823`）；bandit 收紧到 `-ll`
  （误报加 `# nosec` 并注明原因）；`pip-audit` 改为阻断，可用仓库变量
  `PIP_AUDIT_IGNORE` 逐条豁免；覆盖率实测基线 79%，闸门 `--fail-under=70` 改为阻断；
  pre-commit 增加 ruff hook。
- `check --deploy` 的 4 项 HTTPS 告警属于内网 HTTP 部署的有意取舍，仍只留痕不阻断。

### 变更（前端）

- `records/dashboard.html`、`knowledge_base/document_detail.html` 的内联脚本抽离到
  `static/js/pages/`；全站 10 处 `onclick=` 改为事件委托（模板内 `onclick` 归零），
  为后续 CSP enforce 消除唯一硬阻塞。

### 未完成（已评估、留待后续）

- `samples/views.py` 的 15 个手写 CRUD 迁移到 `records/crud.py` 工厂：现有工厂不支持
  samples 的 service 层钩子、`ValidationError`→表单错误、`BusinessError` 文案与
  删除确认页，硬迁移会改变用户可见行为，故本轮不动。

## [2.0.0] - 2026-10-23

### 变更（Breaking，部署形态）

- **调度器改为独立进程**：`runserver`（web）与 `manage.py run_scheduler`（定时任务 + 通知发送）
  各跑一个进程。`NOTIFICATIONS_SCHEDULER_INPROCESS` 默认 `False`，不再随 `DEBUG` 联动。
  升级后必须同时拉起 `run_scheduler`，否则定时任务（到期提醒、每日未完成提醒、保险续保提醒、
  每日自动备份）不会执行。
- 调度存活判定改为跨进程心跳文件 `logs/.hb_scheduler` / `logs/.hb_worker`，
  健康页在独立进程模式下可正确显示「运行中」。

### 变更（安全）

- 生产运行统一使用 `DJANGO_DEBUG=0`；`start.sh` 与 `scripts/windows/start_windows.bat`
  均已同步（此前脚本写 0、README 写 1，文档与实现不一致）。
- README 不再公开验收账号的明文口令。
- 新增 `LICENSE`（Proprietary / All rights reserved）：仓库当前不公开。

### 升级

- Django `4.2.24` → `5.2.18`（4.2 LTS 扩展支持已于 2026-04 结束；5.2 LTS 支持至 2028-04）。
- Django REST Framework `3.17.1` → `3.18.3`（3.18 起要求 Django >= 5.2）。
- requests `2.31.0` → `2.34.2`（修复 CVE-2024-35195 / CVE-2024-47081）。
- cryptography `42.0.2` → `50.0.2`；Pillow `11.0.0` → `13.0.0`。
- openpyxl `3.1.5`、xlrd `2.0.2`、bleach `6.4.0`、markdown `3.11.2`、qrcode `8.2`、
  beautifulsoup4 `4.15.0`。
- 新增 `requirements-dev.txt`，CI 质量工具（ruff/bandit/pip-audit/coverage）锁定版本。

### 修复

- **备份恢复兼容性**：升级前（Django 4.2）产生的备份包此前会被「Django 主版本不兼容」直接拒绝。
  现改为只拒绝「包版本高于当前版本」的降级恢复；旧主版本包允许向前恢复并记录告警
  （表结构与迁移记录一致，`migrate --check` 已验证无待补齐项）。
- `work_record/settings.py`：日志目录创建失败时的降级分支此前不可达（`os.makedirs`
  在导入期先执行且未捕获异常），现改为显式 `_LOG_DIR_OK` 判定后降级到 console handler。
- `records/crud.py`：标准增改表单工厂补传 `request.FILES`（当前模型无文件字段，属防御性修复）。

## [1.0.0] - 2026-10-22

- 全维度审计报告 5 批次整改：安全收口、可访问性、工程门禁、前端收尾、运维加固。
