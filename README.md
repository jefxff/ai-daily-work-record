# AI Daily Work Record 项目

## 项目概述
这是一个基于Django的每日工作记录管理系统，包含装修管理、客户账期、客户客诉、制度及处罚、核价管理等功能。

## 技术栈
- **后端**: Django 5.2.18（LTS）+ Django REST Framework 3.18
- **前端**: Bootstrap 5 + Chart.js
- **数据库**: SQLite
- **API**: Django REST Framework

## 项目结构
```
项目根目录/
├── scripts/           # 工具脚本目录
│   └── windows/       # Windows专用启动脚本
├── logs/              # 日志文件目录（.gitignore 忽略）
├── work_record/       # Django项目配置
├── accounts/          # 认证与RBAC权限模块
├── records/           # 主应用模块（装修/客诉/核价等）
├── knowledge_base/    # 知识库模块
├── samples/           # 试样管理模块
├── archive/           # 档案管理模块（柜/层/格定位、借阅、二维码）
├── ai_api/            # AI API模块
├── sysbackup/         # 数据备份与恢复模块（系统管理员专属）
├── static/            # 静态资源
└── media/             # 用户上传文件
```

## 快速开始

### 环境要求
- Python 3.11+
- Django 5.2.18
- 其他依赖见 requirements.txt

### 安装依赖
```bash
pip install -r requirements.txt
```

### 启动项目
- Windows 一键启动: `scripts/windows/start_windows.bat`（会同时拉起 web 与调度进程）
- 直接启动（推荐）:
  ```bash
  python manage.py collectstatic --noinput    # 静态资源（DEBUG=0 下由 STATIC_ROOT 提供）
  python manage.py runserver 0.0.0.0:8080 --noreload   # 终端 1：web
  python manage.py run_scheduler                       # 终端 2：定时任务 + 通知发送
  ```
  > `--noreload` 必须加：备份/恢复任务跑在 web 进程的后台线程里，auto-reload 会在
  > 保存代码的瞬间重启进程，把正在执行的恢复打断成半状态。改代码后手动重启即可。
- 访问地址: http://127.0.0.1:8080/  （登录页 /accounts/login/）

> `run_scheduler` 是常驻进程，负责到期提醒、每日未完成提醒、保险续保提醒、每日自动备份
> 以及站内/邮件发送。**只启动 runserver 不会执行任何定时任务，且页面不会报错。**

### RBAC 验收账号
项目内置 4 个验收账号，**不再使用任何固定口令**：

- `rbac_root` — 超级管理员（`is_superuser`，恒为全权限）
- `rbac_admin` — 管理员（绑定内置角色 `admin`）
- `rbac_staff` — 普通员工（绑定内置角色 `staff`）
- `rbac_viewer` — 只读用户（绑定内置角色 `viewer`）

口令由一次性初始化命令生成随机值，并写入仓库之外的文件：

```bash
python manage.py rotate_default_passwords --dry-run   # 先看有哪些账号需要轮换
python manage.py rotate_default_passwords             # 轮换仍在使用历史公开口令的账号
python manage.py rotate_default_passwords --force     # 额外强制轮换上述 4 个内置账号
```

新口令默认落盘到 `%USERPROFILE%\WorkRecordBackups\seed_passwords_<时间>.txt`
（或 `--out` 指定路径），请立即转存密码管理器后删除该文件；之后可通过
`/accounts/users/` 重置为个人口令。

角色权限以 `accounts/registry.py` 为唯一事实源；权限条数会随模块增删而变化，
请以 `python manage.py sync_rbac` 输出的实测值为准，本文档不再硬编码具体数字。

「系统管理」模块除标准 CRUD 外还有两个**高危附加动作**，与 `system.read` /
`system.update` 解耦、单独授权，默认只随内置 `admin` 角色发放：

- `system.backup` — 下载备份包。备份包含全库数据 + 全部附件，等于绕过所有模块级
  read 权限的横向导出通道，因此不该被「只为了看服务健康状态」的用户拿到；
- `system.restore` — 触发全量恢复。会用备份包整体覆盖线上数据库，不可逆。

新增模块或新增这类动作后，执行 `python manage.py sync_rbac` 同步权限码；
已有内置角色不会被覆盖，如需对齐默认定义加 `--reset-builtins`。

长期 Token（`/api-token-auth/`）的明文**不落库**：签发时只存哈希与前缀，
明文仅在签发响应里回显一次，历史列表只能看到前缀；备份包外流不再等于凭据外流。

注册账号默认 `is_active=False`，需管理员在 /accounts/users/ 审核激活。

## 功能模块

### 核心功能
1. **装修管理** - 装修项目记录和管理
2. **客户账期** - 客户信用评估和账期管理
3. **客户客诉** - 客户投诉处理记录
4. **制度及处罚** - 公司制度和处罚记录
5. **核价管理** - 价格评估和供应商管理
6. **金属价格** - 实时金属价格监控
7. **档案管理** - 档案柜/层/格物理定位、电子档案、借阅归还、二维码、批量导入导出（验收前仅超级管理员可见）
8. **日志中心** - `logs/` 运行日志增量入库、按级别/文件/关键字/时间窗检索、手动采集与 CSV 导出（左侧「系统」分组，仅管理员）

### 档案管理模块说明

- **入口**：左侧「试样管理」→「档案管理」（仅超级管理员可见）
- **核心功能**：
  - 档案总览：仪表盘（柜数/档案数/空位数/占用率/逾期数）+ 按柜展示层×格网格，颜色区分已存放/空位/已借出，点击空位直接录入
  - 档案详情：完整路径、基本信息、电子文件预览下载、位置二维码、借阅历史、操作历史
  - 档案柜 CRUD：支持 2/4/6/8 层、每层格数自定义
  - 档案 CRUD + 跨空位移动（自动检测目标格占用）
  - 树形类别管理 + 标签检索
  - 借阅登记/归还，逾期自动标记，状态筛选
  - CSV 批量导入导出、操作日志全链路追溯
- **权限**：验收前通过 `accounts/registry.py` 的 `SUPERUSER_ONLY_MODULES` 限制仅超管可见；验收后从该列表移除 `archive` 并执行 `python manage.py sync_rbac` 即可对普通角色开放
- **数据模型**：Cabinet / Category / Archive / BorrowRecord（操作日志复用 records.OperationLog）

### API接口
项目提供REST API接口，详见 `ai_api/` 模块。

## 开发说明

### 代码规范
- 遵循PEP 8编码规范
- 类型提示是渐进目标：新增/改动函数优先补注解，存量代码不强制一次性补齐
- 添加适当的注释和文档

### 测试
运行测试：`python manage.py test`

### 部署

本项目按**两进程**形态部署（Windows 常驻进程直接跑 `runserver` + `run_scheduler`）。

**启动步骤**
```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py createcachetable   # 登录失败锁定使用 DatabaseCache，需先建缓存表

# 静态资源：DEBUG=0 时 /static/ 由 STATIC_ROOT 提供，必须先收集
python manage.py collectstatic --noinput

# 终端 1：web（默认 DJANGO_DEBUG=0，无需显式设置；--noreload 见下）
python manage.py runserver 0.0.0.0:8080 --noreload
# 终端 2：定时任务 + 通知发送（必须常驻，否则定时任务静默停摆）
python manage.py run_scheduler
```
（直接运行 `scripts/windows/start_windows.bat` 或 `./start.sh` 时会自动拉起这两个进程。）

**部署形态说明**
- **web 与调度分离**：`NOTIFICATIONS_SCHEDULER_INPROCESS` 默认 `False`，web 进程内不再起
  调度线程；定时任务由 `run_scheduler` 承担。仅排障时临时设 `DJANGO_SCHEDULER_INPROCESS=1`，
  此时**不要**再启动 `run_scheduler`（否则同一任务会被执行两次）。
- **跨进程心跳**：`run_scheduler` 每 30s 写 `logs/.hb_scheduler` / `logs/.hb_worker`，
  web 进程读文件判断存活，健康页 `/system/health/` 因此能正确显示独立进程状态。
- **备份/恢复任务状态**（`sysbackup`）仍为进程内实现，因此 web 侧必须单进程运行；
  不要用 gunicorn/uwsgi 多 worker 启动。
- 登录失败计数使用 DatabaseCache 落库，跨服务重启仍生效，无法通过"重启"绕过锁定。
- 数据库为 SQLite（已开启 WAL），仅适合局域网单机中小并发；如需横向扩展须先改造任务状态存储。
- **DEBUG 默认关闭（0）**：未设置 `DJANGO_DEBUG` 时即为 `0`，避免未捕获异常把含 `SECRET_KEY`
  的配置快照打到 500 页。排障时可临时设 `DJANGO_DEBUG=1`，排障后务必改回 0。
- **ALLOWED_HOSTS 自动探测**：默认取本机主机名与局域网 IP（换网段自动跟随），
  可用 `DJANGO_ALLOWED_HOSTS`（逗号分隔）追加；`DJANGO_DEBUG=0` 时禁止通配符 `*`（启动即报错）。
- **静态文件**：`DJANGO_DEBUG=0` 时 runserver 不再自动服务 `/static/`。项目在
  `work_record/urls.py` 显式挂载：优先从 `STATIC_ROOT`（collectstatic 产物）读取，
  只有尚未 collectstatic 时才回退到 staticfiles finders 并打印一条告警。
  启动脚本已内置 `collectstatic`；CI 另有「产物非空」门禁，防止静默丢样式。
- **导出保护**：单进程 runserver 无并发上限/超时，导出（openpyxl 全量内存构建）用两个
  设置兜底——`EXPORT_MAX_CONCURRENCY`（默认 2，超出 429）与 `EXPORT_MAX_ROWS`
  （默认 30000 行，超出 413 并提示缩小筛选范围）。
- **备份保留与密钥**：每日备份按 `BACKUP_RETAIN_DAILY`（默认 14 天）+ 每周 1 份 ×
  `BACKUP_RETAIN_WEEKLY`（默认 8 周）自动轮转；加密密钥默认在
  `%LOCALAPPDATA%\WorkRecordKeys\.backup_key`（非备份目录）。**密钥必须与备份分开保管**，
  否则磁盘故障时两者一起丢失，历史加密包将永久无法解密。
- **数据恢复维护模式**：恢复期间（含换库与附件解压）非超管的写请求返回 503，
  恢复结束自动解除；恢复中途失败会自动回滚到 `backups/safety_snapshots/` 的安全快照。

### HTTPS 部署（内网自签证书，可选）

明文 HTTP 下同网段可嗅探会话 Cookie。若不接受该风险，按下述方式启用 HTTPS：

```bash
# 1) 生成本地 CA + 服务器证书（SAN 自动包含 localhost / 回环 / 主机名 / 内网 IP）
python scripts/gen_dev_cert.py
# 2) 把本地 CA 装进受信任根证书（管理员权限执行，只需一次；之后续签的证书仍受信）
certutil -addstore -f "ROOT" certs\ca.crt.pem
# 3) HTTPS 方式启动（web + 调度两进程）
scripts\windows\start_windows_https.bat      # Windows
./start_https.sh                              # Linux/macOS
# 访问 https://127.0.0.1:8443/
```

实现说明（为什么不用现成方案）：
- `runserver` 自 Django 1.9 起不再支持 `--cert-file`；waitress 3.x **没有** TLS 支持；
  gunicorn/uwsgi 在本项目属禁用项（任务状态/互斥/心跳是进程内实现）。
- `scripts/serve_https.py` 用标准库 `wsgiref` + `ssl.SSLContext` 提供**单进程多线程**的
  HTTPS 服务，HTTP/1.1 keep-alive，静态/媒体仍走 Django 自己的路由，行为与 HTTP 版一致。
- 启动脚本内部设 `DJANGO_HTTPS_ONLY=1`：启用 `SECURE_SSL_REDIRECT`、Secure Cookie 与
  `__Host-` 前缀，`check --deploy` 的 W008/W012/W016 随之消失。
- HSTS 单独用 `DJANGO_HSTS_SECONDS` 放量（默认 0，不发送）；确认全站 HTTPS 后再开，
  否则浏览器记住 HSTS 且证书出问题时，回退会很痛苦。
- 证书与 CA 私钥在 `certs/`（已 gitignore）。**CA 私钥不要外发**；装信任的机器只需
  `ca.crt.pem`。换 IP/续签：`python scripts/gen_dev_cert.py --force`。

> 若将来并发显著上升，建议前置 stunnel/nginx 终止 TLS，本脚本即可停用。

### 备份异地副本（C2）

备份目录与数据库同在一块盘时，磁盘故障会同时带走两者。配置镜像目录后，每次备份
完成会自动复制一份过去（失败只记警告，不影响本次备份）：

```bash
# Windows（多个目录用分号分隔）
set BACKUP_MIRROR_DIRS=D:\WorkRecordMirror;\\nas\backup\workrecord
# Linux/macOS
export BACKUP_MIRROR_DIRS=/mnt/usb/workrecord:/mnt/nas/workrecord
```

复制结果会写进备份任务详情与操作留痕（`异地副本 n/m 成功`）。建议镜像目录选
**另一块物理盘 / 移动硬盘 / 另一台机器**，不要把镜像放回同一块盘。

### 依赖完整性与漏洞审计（C3）

```bash
pip install --require-hashes -r requirements.lock     # 可复现安装（存在锁文件时优先）
python -m pip_audit -r requirements.txt               # 漏洞审计（需联网 PyPI 顾问库）
```

`requirements.lock` 由 `scripts/gen_requirements_lock.py`（pip-tools）生成，含每个包的
sha256；升级依赖后需重新生成并提交。CI 已在每次 push 执行 pip-audit 并阻断已知漏洞。

### 多进程/多实例改造（C4）

当前架构**明确依赖单进程**。若将来要换多 worker 或多实例，请先按
`docs/multi-worker-migration.md` 的清单改造（任务状态外置、互斥改 DB 级、导出闸门换
分布式计数、备份恢复独立成命令），不要“先上多进程再说”。

### 开发约定（横切规则）

留痕词表、响应外壳、状态码、权限写法、分页、上传校验、导出闸门、时间、日志、事务
这十类横切规则统一写在 **`docs/api-conventions.md`**，新增模块照此执行。
配套的单一出处：

| 能力 | 模块 |
|---|---|
| 留痕模块名/动作名/同义词 | `records/audit_vocab.py` |
| 留痕写入（唯一入口） | `records/audit.py::log_operation` |
| 文件头校验（唯一表） | `work_record/file_validation.py` |
| 导出并发闸门 | `work_record/export_guard.py::guarded_export` |
| 账户/角色共享规则 | `accounts/user_admin.py` |

**自动化校验（CI）**
`.github/workflows/ci.yml` 在 push / PR 到 `main` 时执行：
`pip install -r requirements.txt` → `pip install -r requirements-dev.txt` → `pip check` →
`manage.py check` → `check --deploy`（生产形态）→ `makemigrations --check` →
ruff / bandit / pip-audit → `coverage run manage.py test`。所有 Action 固定到 commit SHA，
质量工具版本锁定在 `requirements-dev.txt`。

## 安全与合规说明

- **密钥管理**：现网 `SECRET_KEY` 由首次启动生成到 `.secret_key`（已 gitignore），
  与 git 历史提交 `5d3f40f` 中的硬编码值不同，历史密钥**已作废**，任何旧部署都必须重新生成。
  仓库不保存 `.env` / `.secret_key` / 私钥；`.pre-commit-config.yaml` 启用 gitleaks 阻止密钥再次入库。
- **语言策略**：本项目面向中文单语内网用户，**不做 i18n 多语言改造**（`USE_I18N` 保留 Django 默认行为，
  模板不使用 `{% trans %}`）；如未来需要多语言，再统一抽取 `{% trans %}` 与 `locale/`。
- **依赖锁定**：`requirements.txt` 为精确版本；带哈希的 `requirements.lock` 需在可联网环境执行
  `pip-compile --generate-hashes` 生成后使用 `pip install --require-hashes -r requirements.lock`。
- **备份加密**：备份包默认以 Fernet 加密为 `.zip.enc`，密钥来自 `BACKUP_KEY` 环境变量或 `.backup_key` 文件；
  旧 `.zip` 明文包仍可读取。默认备份目录为 `%USERPROFILE%\WorkRecordBackups`，可用 `BACKUP_DIR` 覆盖。


**档案管理验收后开放普通角色权限**
```bash
# 1. 编辑 accounts/registry.py，从 SUPERUSER_ONLY_MODULES 移除 "archive"
# 2. 同步权限到数据库
python manage.py sync_rbac
```

## 维护说明

### 数据备份
定期备份数据库文件 `db.sqlite3`

### 日志管理
日志文件保存在 `logs/` 目录。左侧「系统」→「日志中心」（`/system/logs/`）会把白名单文件
（`LOG_INGEST_FILES`，默认 `debug.log*`、`csp_violations.log`、`runserver*.log` 等）的增量内容
解析成结构化日志入库，支持级别 / 文件 / 关键字 / 时间窗检索与 CSV 导出。页面打开时按
`LOG_INGEST_THROTTLE_SECONDS`（默认 60s）节流采集，也可点「采集最新」立即拉取；数据按
`LOG_RETENTION_DAYS`（30 天）与 `LOG_MAX_ROWS`（10 万条）自动裁剪，入库前对
`password/token/SECRET_KEY` 形态做掩码。采集状态保存在 `logs/.log_ingest_state.json`（不进 Git）。

### 监控
监控系统运行状态和性能指标

---
**项目版本**: 2.0.0  
**最后更新**: 2026-10-23  
**维护团队**: 开发团队

**许可**：Proprietary / All rights reserved，详见 `LICENSE`。仓库当前不对外公开。
