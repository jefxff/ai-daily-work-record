# AI Daily Work Record 项目

## 项目概述
这是一个基于Django的每日工作记录管理系统，包含装修管理、客户账期、客户客诉、制度及处罚、核价管理等功能。

## 技术栈
- **后端**: Django 4.2.24
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
- Python 3.8+
- Django 4.2.24
- 其他依赖见 requirements.txt

### 安装依赖
```bash
pip install -r requirements.txt
```

### 启动项目
- Windows 一键启动: `scripts/windows/start_windows.bat`
- 直接启动（推荐）: `python manage.py runserver 0.0.0.0:8080`
- 访问地址: http://127.0.0.1:8080/  （登录页 /accounts/login/）

### RBAC 验收账号
项目内置 4 个验收账号（密码均为 `X12345678`）：
- `rbac_root` — 超级管理员（`is_superuser`，恒为全权限）
- `rbac_admin` — 管理员（绑定内置角色 `admin`）
- `rbac_staff` — 普通员工（绑定内置角色 `staff`）
- `rbac_viewer` — 只读用户（绑定内置角色 `viewer`）

角色权限以 `accounts/registry.py` 为唯一事实源；权限条数会随模块增删而变化，
请以 `python manage.py sync_rbac` 输出的实测值为准，本文档不再硬编码具体数字。

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
- 使用类型提示
- 添加适当的注释和文档

### 测试
运行测试：`python manage.py test`

### 部署

本项目按**单进程**形态部署（Windows 常驻进程直接跑 `runserver`）。

**启动步骤**
```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py createcachetable   # 登录失败锁定使用 DatabaseCache，需先建缓存表
# 本机/内网运行：显式开启调试模式（保留静态文件自动服务与详细报错）
set DJANGO_DEBUG=1                  # Windows；Linux/macOS 用 export DJANGO_DEBUG=1
python manage.py runserver 0.0.0.0:8080
```
（直接运行 `scripts/windows/start_windows.bat` 或 `./start.sh` 时，脚本已自动设置 `DJANGO_DEBUG=1`。）

**部署形态说明**
- **必须单进程运行**：备份/恢复任务状态（`sysbackup`）与消息通知调度线程均为进程内实现，
  启动多 worker（gunicorn/uwsgi）会导致任务状态互不可见、同一时刻仅一个任务的互斥失效。
- 登录失败计数使用 DatabaseCache 落库，跨服务重启仍生效，无法通过"重启"绕过锁定。
- 数据库为 SQLite（已开启 WAL），仅适合局域网单机中小并发；如需横向扩展须先改造任务状态存储。
- **DEBUG 默认关闭（0）**：未设置 `DJANGO_DEBUG` 时即为 `0`，避免未捕获异常把含 `SECRET_KEY`
  的配置快照打到 500 页；本机/内网运行请显式设 `DJANGO_DEBUG=1`。
- **ALLOWED_HOSTS 自动探测**：默认取本机主机名与局域网 IP（换网段自动跟随），
  可用 `DJANGO_ALLOWED_HOSTS`（逗号分隔）追加；`DJANGO_DEBUG=0` 时禁止通配符 `*`（启动即报错）。
- `DJANGO_DEBUG=0` 时 runserver 不再自动服务 `/static/`，项目已在 `work_record/urls.py`
  显式挂载静态文件路由，内网用户页面样式不受影响。

**自动化校验（CI）**
`.github/workflows/ci.yml` 在 push / PR 到 `main` 时执行：
`pip install -r requirements.txt` → `pip check` → `manage.py check` →
`check --deploy`（生产形态）→ `makemigrations --check` → ruff / bandit / pip-audit →
`coverage run manage.py test`。所有 Action 固定到 commit SHA，Dependabot 每周检查依赖更新。

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
日志文件保存在 `logs/` 目录

### 监控
监控系统运行状态和性能指标

---
**项目版本**: 1.0.0  
**最后更新**: 2026-10-22  
**维护团队**: 开发团队
