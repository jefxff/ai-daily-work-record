# 批次 A 验收清单（checklist）

对应规格：[spec.md](./spec.md)；任务：[tasks.md](./tasks.md)。
本清单只记录**可核验**的事实；未能取得直接证据的项已在条目内显式标注。

## 一、硬约束

- [x] 批次 A **不新增/修改任何 model 字段**；`git status` 中 models.py 与业务迁移文件均无改动
- [x] 唯一新增迁移为 `notifications/migrations/0005_seed_task_failure_template.py`（`RunPython` 数据迁移，播种通知模板）
- [x] `python manage.py makemigrations --check --dry-run` → `No changes detected`（模型与迁移一致，无待生成迁移）
- [x] 未运行数据库 schema 变更；全程未删除 `db.sqlite3-wal` / `db.sqlite3-shm`

## 二、功能落地

- [x] A1 到期风暴：`records/expiry.py` 归一 4 类来源（insurance / archive_contract / license_expire / license_review），`records/views_expiry.py` 提供日历页与导出
- [x] A1 日历页按天分组排列，红/黄/绿 severity 分级（已过期或 ≤7 天 = danger，提前期内 = warning，其余 = success）
- [x] A1 支持按类型分别配置提前天数（`lead_insurance` 30 / `lead_contract` 60 / `lead_license` 60 / `lead_review` 30，均可通过 GET 覆盖）
- [x] A1 「本月到期清单」xlsx 导出：经 `sanitize_xlsx_formulas` 公式注入防护 + `content_disposition` 中文文件名；空数据返回「本月暂无到期事项」
- [x] A1 跨模块权限：视图内逐来源按 module `read` 过滤；无任一 read → 403；新增 `@perm_required_any` 作为声明式兜底
- [x] A2 周报生成：`/daily_record/weekly_report/` 仅本人（`_my_daily_records`），按 完成/进行中/未完成 三分组，输出 Markdown，提供「复制到剪贴板」
- [x] A2 AI 润色仅预留开关位 `settings.WEEKLY_REPORT_AI_ENABLED`，本批不接入 AI（`maybe_ai_polish` 恒返回原文）
- [x] A3 年度个人总结 `/daily_record/annual_summary/`：聚合 记录数/完成数/完成率/平均进度/涉及部门数/覆盖月份数/最长连续记录天数，渲染可分享卡片 + 复制文本
- [x] A3 未杜撰「参与项目数」（`DailyRecord` 无项目字段），以部门数/月份数/连续天数替代
- [x] A4 定时任务连续失败告警：`scheduler._handle_task_result` 在连续失败达 `FAIL_STREAK_THRESHOLD=3` 时通知 `system.update` 管理员，同日同任务经 `dedup_context` 幂等只发一次；成功即清零；发送失败仅记日志不影响任务主流程
- [x] A4 告警模板 `scheduled_task_failure` 经数据迁移播种（迁移后 `NotificationTemplate` 8→9）
- [x] A4 `_FAIL_STREAK` 为进程内计数，限制（重启清零、仅「本次运行内」语义）已在模块 docstring 与常量旁显式注明
- [x] A5 审计异常行为检测 `/operation_log/anomalies/`：删除爆发 > 20 / 非工作时间（工作日 8–18 点外）批量 ≥ 10 / 单小时高频 > 50，全部 ORM 聚合，纯读无写入
- [x] A5 仅具备 `system.read` 者可访问（装饰器 `@perm_required("system","read")`）
- [x] A5 「导出/下载行为专项追踪」明确不在本页（现有留痕无导出类型），页面内已注明属批 B
- [x] A6 系统健康面板 `/system/health/`：健康探测三项 + 调度心跳（worker/scheduler + HEARTBEATS 时间戳）+ 定时任务表 + 备份操作历史（近 20 条）+ 磁盘占用（总/已用/可用/百分比 + db.sqlite3 大小 + backups / MEDIA_ROOT 目录大小）
- [x] A6 探测逻辑抽取为 `work_record/health.py: probe_health()`，`ai_api.views.health_check` 改为复用；**对外 payload 键与状态码规则保持不变**（有单测断言 `probe_health` 与 `health_check` 输出一致）
- [x] A6 页面内标注「请求量 / P95 / 慢查询属于批 C」非目标提示区
- [x] 侧栏新增 5 个入口：到期预警、周报、年度总结、异常检测、系统健康（并按权限正确显隐）

## 三、前端规范

- [x] 5 个新模板均 `{% extends %}` 项目基座模板（`records/base.html`），复用既有 `.table-container` / `.table` / `.status-badge` / `btn-transparent` 体系
- [x] 新增模板 `extra_css` 仅使用设计令牌（`var(--c-*)`），无裸色值、无内联 `style` 属性
- [x] 列表/卡片容器未设置固定 max-width（大屏全屏展示，不产生两侧留白）
- [x] `<script>` 块内未出现需加 `//` 前缀的 Django 权限标签（无同类语法风险）

## 四、回归与数据完整性

- [x] `python manage.py check` → `System check identified no issues (0 silenced)`
- [x] 5 个新模板 `get_template()` 编译全部通过
- [x] `python manage.py test` 全量 **254 项通过，0 失败**（含 a7-fix 修复后的 `accounts.tests.RbacAuthTests.test_ac12_coverage_completeness`）
- [x] 新增专项测试：`records/tests_expiry.py`（4）、`records/tests_audit.py`（5）、`notifications/tests_task_failure.py`（3）、`notifications/tests_health.py`（4）
- [x] 全表记录数基线核对：迁移前后逐项比对 **41 张业务/系统表**，唯一差异为 `NotificationTemplate` 8 → 9（A4 播种模板），其余 40 张表计数**完全一致**
- [x] 全站 RBAC 静态覆盖审计通过：`records.urls` 全部视图均带 `rbac_permission`（跨模块页以 `perm_required_any` 声明）；零权限用户遍历 6 个新 URL 无一返回 200

## 五、浏览器实测（有缺口，已如实标注）

- [x] `/expiry/calendar/` 真实浏览器验证通过：摘要徽章与按天分组表格正常渲染，侧栏「到期预警」高亮，无样式崩坏
- [x] `/expiry/export/` 真实浏览器验证通过：未返回 404/500/403，行为符合直接下载 xlsx
- [x] `/daily_record/weekly_report/` 真实浏览器验证通过：周次切换、统计卡、Markdown 区、复制按钮、侧栏高亮均正常
- [x] `/daily_record/annual_summary/` 真实浏览器验证通过：年份选择器、指标卡片、TOP 部门区块、侧栏高亮均正常
- [x] `/operation_log/anomalies/` Playwright（Firefox 1440×900，已登录态）实测通过：HTTP 200、标题「异常检测」、四类异常分区与表格正常渲染、侧栏「异常检测」高亮、浏览器控制台零错误；截图 `tests/visual/test-results/anomalies.png`
- [x] `/system/health/` Playwright（Firefox 1440×900，已登录态）实测通过：HTTP 200、标题「系统健康」、健康探测/调度心跳/定时任务/备份历史/磁盘占用五区块完整渲染、侧栏「系统健康」高亮、浏览器控制台零错误；截图 `tests/visual/test-results/health.png`
- [x] 浏览器控制台出现的 `net::ERR_CONNECTION_RESET` / `ERR_CONNECTION_REFUSED`（指向 `/notifications/api/unread/`）已定位为 runserver 自动热重载期间的历史瞬时消息（服务监听进程 PID 由 3680 变为 27192），非新代码缺陷

## 六、决策点落地情况（对应 spec.md 第 7 节）

- [x] D1 A1 采用「按天分组列表」而非月历网格
- [x] D2 A2/A3 范围仅本人，不提供部门范围
- [x] D3 A4 连续失败计数用进程内 `_FAIL_STREAK`（零 schema，重启清零）
- [x] D4 A5 不含「导出行为追踪」，推迟批 B
- [x] D5 A6 不含请求量 / P95 / 慢查询，推迟批 C
- [x] D6 健康页 URL = `/system/health/`
- [x] D7 侧栏新增 5 个入口
- [x] 补充：spec.md 中 A4 迁移编号 `0003` 为笔误，实际为 `0005`（0003 / 0004 已被占用）

## 七、遗留与已知限制

- A4 连续失败计数为进程内状态：服务重启即清零，语义为「本次运行内连续失败」；如需跨重启累计，需在批 B 增加 `fail_streak` 字段与迁移（已知限制，非遗留任务）
- [x] 批次 A 改动已提交并推送至 `origin/main`（远端 HEAD `c5bc94d`）
