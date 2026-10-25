# 变更日志

本文件记录对外可见的功能、部署形态与依赖变更。格式参考 Keep a Changelog。

## [2.1.3] - 2026-10-25

变更一致性核查后的收敛：消除重复实现、修复两处真实权限缺陷。**无 URL、模板引用、
数据与部署步骤变化**。

### 重构（消除重复实现）

- 新增 `accounts/user_admin.py`：用户/角色的**共享规则层**（超管保护、`is_staff`
  仅超管可授、密码强度校验、角色编码唯一、权限码合法性、内置角色锁定）。
  `accounts/views_admin.py`（页面）与 `ai_api/api_admin.py`（DRF）都改为调用它，
  不再各写一遍 —— 此前两份实现语义一致但存在漂移风险。
  与 `accounts/services.py`（RBAC 只读判定）职责分离，互不依赖。
- `records/audit.py` 新增 `write_operation()` 与 `operator_from_request()`，成为
  **全站唯一的留痕写入点**。`samples`、`archive`（含证照）与 `sysbackup` 原先各自
  `OperationLog.objects.create(...)`，现全部收敛（`BackupOperationLog` 是另一个
  模型，保留）。操作人展示口径不变：档案/证照仍优先用 Profile 姓名。
- 删除 DRF 侧死代码 `_perms_by_codes()`（其调用点早已改为整体替换）。

### 修复（权限，均为 API 侧）

- **`custom_action_perms` 替换而非合并标准动作映射**：声明了自定义动作权限的视图集，
  `list/retrieve/create/update/destroy` 会全部落到「未映射 → 拒绝」，导致**拥有
  admin 角色的非超管调用用户管理 API 一律 403**。改为与 `ACTION_MAP` 合并。
  影响面：`accounts/users`、`accounts/api-tokens`、`notifications/tasks` 等全部
  自定义动作视图集。
- **PATCH 更新用户未做越权校验**：`_guarded()` 只在部分 action 里调用，
  `partial_update` 可绕过「非超管不得操作超管账号」直接改超管账号（页面端是 403）。
  改为在 `get_object()` 统一拦截，新增 action 不会再漏。
- **PATCH 角色会清空权限**：`RoleLiteSerializer.to_internal_value()` 无条件注入
  `permission_codes=[]`，任何「只改名称/启停」的局部更新都会把权限清空。改为仅在
  请求显式携带该字段时注入。

### 测试

- 全量 **603 个测试通过**（新增 3 个：角色局部更新不清权限、非超管不能授予
  `is_staff`、非超管不能改超管账号）；`check` 无问题；`makemigrations --check`
  无变化；ruff 全绿；bandit medium 及以上无问题。

## [2.1.2-hotfix3] - 2026-10-25

### 修复（备份中心页面的两个显示/可见性问题）

- **模板注释被当成正文渲染**：`{# ... #}` 只支持单行，多行注释必须用
  `{% comment %}...{% endcomment %}`。此前的多行说明直接显示在页面顶部。
- **无法解密的备份包会从列表"消失"**：`_read_backup_summary` 出错时原来 `return None`，
  用户只看到备份数量变少却不知原因。现在返回降级条目（时间取文件 mtime、操作人显示
  「无法读取」），页面打红色「无法解密」徽标并**只保留删除按钮**，恢复/下载入口不再渲染。
- 顺带修正：多处服务重启未真正替换进程（Windows 下 `SO_REUSEADDR` 允许多进程监听同一端口，
  旧进程仍在响应请求），现已确保每个角色只有 1 个进程。核对方法：
  `netstat -ano | findstr :8080` 只应有一行 LISTENING。

### 测试

- 全量 600 个测试通过（新增「坏包必须可见并标记」用例）；ruff 全绿。

## [2.1.2-hotfix2] - 2026-10-25

### 修复（备份密钥可能丢失 → 产生永远打不开的备份包）

事故链：端口上残留了多个旧版本服务进程（Windows `SO_REUSEADDR` 允许重复监听，
连接落到哪个进程不确定），旧代码把加密密钥重新生成到备份目录且**未校验落盘**，
导致 15:07 / 15:15 两个备份包用了一把事后不存在的密钥加密，永久无法解密。

- `services._fernet()`：生成新密钥后**必须写入成功并回读校验**，否则抛
  `RuntimeError` 中止本次备份——宁可没有新备份，也不产生打不开的包。
- `services._decrypt_fernets()` / `_decrypt_file()`：解密时依次尝试主密钥 +
  全部历史密钥（项目根 / 备份目录 / `*.legacy*` 归档），任一把能解开即恢复成功。
  已实测 7 个历史包中 5 个恢复可解（另 2 个确认永久损坏）。
- 现场处置：备份目录里旧进程生成的 `.backup_key` 已隔离为
  `.backup_key.legacy-20261025`（仍参与解密回退），主密钥统一在
  `%LOCALAPPDATA%\WorkRecordKeys\`。
- 清理了残留进程：8080 上曾有 2 个、8443 上 1 个、`run_scheduler` 2 个
  （重复调度会导致通知/备份重复执行），现每个角色仅保留 1 个。
- 新增 2 条回归测试（多密钥解密、密钥无法持久化时中止加密）。

## [2.1.2-hotfix] - 2026-10-25

### 修复（数据恢复入口失效）

- **点「恢复」报「备份包路径无效」**：`sysbackup/templates/sysbackup/backup_center.html`
  的服务端渲染行用 `data-path="{{ b.path|escapejs }}"`。`escapejs` 是 JS 字符串转义，
  会把 Windows 路径的反斜杠变成 `\u005C`，浏览器提交的路径因此不存在的文件
  → 后端 `is_file()` 判否。表现为「刷新页面后点恢复必失败，点一次『扫描』后（JS 渲染行）
  又能成功」这种极难复现的组合。
  改用 `|escape`（HTML 属性专用转义），并补回归测试锁定（测试在旧写法下会红）。

## [2.1.2] - 2026-10-25

审查 C 批（传输安全 / 异地副本 / 依赖完整性 / 多进程边界）。

### 新增（HTTPS）

- `scripts/gen_dev_cert.py`：生成本地 CA + 服务器证书（SAN 自动含 localhost / 回环 /
  主机名 / 内网 IP），装一次根证书后浏览器不再告警，续签仍受信。
- `scripts/serve_https.py`：标准库 `wsgiref` + `ssl.SSLContext` 的**单进程多线程**
  HTTPS 服务（HTTP/1.1 keep-alive），零新增运行时依赖；启动时自动置
  `DJANGO_HTTPS_ONLY=1`（Secure Cookie + `__Host-` 前缀 + SSL 重定向）。
- `start_https.sh` / `scripts/windows/start_windows_https.bat`：HTTPS 启动入口。
- 实测（Python 3.13）：仅在 `WSGIRequestHandler.get_environ()` 改
  `wsgi.url_scheme` 会被 wsgiref 重置，导致配合 `SECURE_SSL_REDIRECT` 无限 301；
  已在应用入口强制注入并在代码里注明原因。
- 开启 HTTPS 后 `check --deploy` 的 W004/W008/W012/W016 全部消失，仅剩
  故意不开启的 W005（includeSubDomains）与 W021（preload）——两者属粘性承诺，
  需显式 opt-in。

### 新增（备份异地副本）

- `BACKUP_MIRROR_DIRS`：备份完成后自动复制到镜像目录（另盘/移动硬盘/网络盘），
  复制后校验大小；失败只记警告与留痕，不影响备份任务成功率。
- 任务详情返回 `mirrors` 结果，操作留痕追加「异地副本 n/m 成功」。

### 新增（依赖完整性）

- `requirements.lock`：直接 + 传递依赖全量 pin（26 个包）。当前版本由干净虚拟环境
  `pip freeze` 生成、**不含 hash**（生成环境无公网 PyPI 访问，取不到官方 wheel 哈希）；
  需要供应链投毒防护时在有公网的环境按 `scripts/gen_requirements_lock.py` 重新生成。
- `scripts/check_lock_sync.py` + CI 阻断步骤：requirements.txt 改了却没更新锁文件时 CI 变红。
- 本地 `pip-audit -r requirements.txt` 实测：**No known vulnerabilities found**。

### 新增（文档）

- `docs/multi-worker-migration.md`：多进程/多实例改造清单（5 处进程内实现、改造顺序、
  必须同步复核的假设），并说明当前单进程是正确选择而非妥协。
- README 增补 HTTPS / 异地副本 / 依赖审计 / 多进程边界四节。

### 测试

- 全量 596 个测试通过（新增异地副本用例）；`check --deploy` 在 HTTPS 模式下仅剩 2 条
  故意保留的告警；ruff 全绿；bandit medium 及以上无问题。

## [2.1.1] - 2026-10-25

代码审查（单机 + SQLite + runserver 场景）整改：A 批 8 项 + B 批 4 项。
全部改动保持 URL、URL 名称、模板引用与业务数据不变（除明确标注的部署步骤）。

### 修复（数据可靠性）

- **SQLite `transaction_mode=IMMEDIATE`**：事务一开始即取写锁，消除 DEFERRED 事务
  「先读后写」锁升级在 runserver 多线程下的偶发 `database is locked`。
- **通知副作用改 `transaction.on_commit`**：样品终止、证照年审未通过两类通知
  在事务回滚后不再误发。
- **恢复失败自动回滚**：换库或恢复附件中途失败时，自动把
  `backups/safety_snapshots/` 的安全快照灌回，不再停在「库已换、附件一半」的半状态。
- **恢复期维护模式**：恢复期间非超管的写请求返回 503，结束后自动解除。

### 修复（运维）

- **备份自动轮转**：`BACKUP_RETAIN_DAILY`（14 天）+ `BACKUP_RETAIN_WEEKLY`（8 周），
  只清理本系统命名的 `backup_*` 包，不动 `safety_snapshots/` 与用户自放文件。
- **备份密钥与备份目录分离**：默认移至 `%LOCALAPPDATA%\WorkRecordKeys\.backup_key`；
  启动时自动从旧位置（项目根 / 备份目录）迁移并删除旧副本，历史加密包仍可解密。
- **审计清理与库维护**：`cleanup_operation_log` 增加登录日志清理（`--login-days`）；
  新增 `db_maintenance` 命令（wal_checkpoint + VACUUM + ANALYZE，28 天节流）；
  两者随迁移 `notifications.0014` 种子为默认启用的每日任务。
- **启动脚本加 `--noreload`**，并内置 `collectstatic`（web/备份恢复线程不再被热重载打断）。

### 变更（性能与容量）

- **公告推送批量化**：收件人偏好一次性预取 + `bulk_create`，50 人公告从 100+ 次
  SQLite 查询/写入降到数次。
- **导出保护**：`EXPORT_MAX_CONCURRENCY`（默认 2，超出 429）与 `EXPORT_MAX_ROWS`
  （默认 30000，超出 413），覆盖通用增删改查导出、样品导出与档案表级导出。
- **静态资源改由 `STATIC_ROOT` 提供**（collectstatic 产物），未收集时回退 finders 并告警；
  CI 增加「产物非空」门禁。

### 变更（部署步骤，需知悉）

- 启动前需执行一次 `python manage.py collectstatic --noinput`（启动脚本已内置）。
- `runserver` 需加 `--noreload`；改代码后手动重启。
- 备份密钥位置改变：若曾把密钥与备份放一起，本次启动会自动迁移并删除旧副本，
  请确认新位置已纳入你的备份/保管流程。

### 测试

- 全量 595 个测试通过（新增 10 个：轮转、密钥迁移、维护模式、导出闸门、种子任务）；
- `manage.py check` 无问题；`makemigrations --check` 无遗漏迁移；
  ruff 全绿；bandit medium 及以上无问题。

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
