# 批次 A 任务清单

对应规格：[spec.md](./spec.md)。每项完成后：`manage.py check` → 定向测试 → 通过再进入下一项。

- [x] **a1 到期风暴日历 + 本月到期清单导出**（零 schema）
  - [x] 新建 `records/expiry.py`：`SOURCE_META`/`DEFAULT_LEAD_DAYS`/`DEFAULT_HORIZON_DAYS`/`LOOKBACK_DAYS`/`URGENT_DAYS`；`collect_expiry_items` / `group_by_day` / `summarize` / `month_items`
  - [x] 新建 `records/views_expiry.py`：`expiry_calendar`（权限自判 403 + GET days/lead 参数）、`expiry_export`（当月 xlsx + 公式注入防护 + content_disposition + 空态）
  - [x] 新建 `records/templates/records/expiry_calendar.html`（摘要条 + 按天分组卡片 + 设计令牌）
  - [x] `records/urls.py` 增加 `expiry_calendar` / `expiry_export` 路由
  - [x] `_sidebar.html` 插入 `{% can_view_any "insurance" "archive" %}` 到期预警入口
  - [x] 定向测试 `records/tests_expiry.py`：无权限 403、来源过滤、severity 分档、导出空态（4 项通过）

- [x] **a2 每日记录 → 周报生成**（模板为主 + AI 开关位）
  - [x] 新建 `records/views_reports.py`：`weekly_report`（本人 + 周首日参数 + 三分组 + Markdown + `maybe_ai_polish` 占位）
  - [x] 新建 `records/templates/records/weekly_report.html`（周切换 + `<pre>` + 复制按钮）
  - [x] `records/urls.py` 增加 `weekly_report` 路由
  - [x] `daily_record_list.html` top_nav 增「生成周报」按钮；侧栏 daily_record 块增「周报」

- [x] **a3 年度个人总结卡片**
  - [x] `records/views_reports.py` 增 `annual_summary`（本人 + year 参数 + 聚合，含 `max_streak`/`active_months`）
  - [x] 新建 `records/templates/records/annual_summary.html`（可分享卡片 + 复制按钮）
  - [x] `records/urls.py` 增加 `annual_summary` 路由；侧栏 daily_record 块增「年度总结」

- [x] **a4 定时任务连续失败告警**
  - [x] 新建 `notifications/migrations/0005_seed_task_failure_template.py`（播种 `scheduled_task_failure`；**编号实为 0005**，因 0003/0004 已被占用，spec.md 中的 0003 系笔误）
  - [x] `notifications/scheduler.py`：`FAIL_STREAK_THRESHOLD`/`_FAIL_STREAK`/`_handle_task_result`；`run_task` 末尾调用
  - [x] 定向测试 `notifications/tests_task_failure.py`：连续失败 3 次触发通知、成功清零（3 项通过）

- [x] **a6 系统健康面板（只读）**
  - [x] 新建 `work_record/health.py: probe_health()`；改 `ai_api/views.py: health_check` 复用（payload 不变）
  - [x] 新建 `notifications/views_health.py: system_health`（`@perm_required('system','read')`；心跳/任务/备份/磁盘）
  - [x] 新建 `notifications/templates/notifications/system_health.html`
  - [x] `work_record/urls.py` 增加 `system/health/` 路由；侧栏系统组增「系统健康」
  - [x] 定向测试 `notifications/tests_health.py`：权限 403、`probe_health` 与 `health_check` 一致（4 项通过）

- [x] **a5 审计异常行为检测（读取层）**
  - [x] 新建 `records/views_audit.py: audit_anomalies`（`@perm_required('system','read')`；删除爆发/非工作时间/高频）
  - [x] 新建 `records/templates/records/audit_anomalies.html`
  - [x] `records/urls.py` 增加 `audit_anomalies` 路由；`operation_log_list.html` 增按钮；侧栏系统组增「异常检测」
  - [x] 定向测试 `records/tests_audit.py`：21 条/小时删除命中、正常量不命中（5 项通过）

- [x] **a7-fix 全站 RBAC 覆盖审计失败项修复**（a7 全量回归时由 `accounts.tests.RbacAuthTests.test_ac12_coverage_completeness` 发现）
  - [x] `accounts/decorators.py` 新增 `perm_required_any(*perms)`（行 76–116）：任一 `(module, action)` 满足即放行，并设置 `rbac_permission` / `rbac_any_module` 供静态审计识别
  - [x] `records/views_expiry.py` 的 `expiry_calendar` / `expiry_export` 改用 `@perm_required_any(("insurance","read"), ("archive","read"))`
  - [x] `records/views_reports.py` 的 `weekly_report` / `annual_summary` 加 `@perm_required("daily_record","read")`
  - [x] `accounts/tests.py` 无角色遍历 URL 列表补充到期预警/周报/年度总结/异常检测/系统健康/到期导出 6 个新页面（仅增强，未改白名单、未删断言）
  - [x] 重跑 `accounts.tests.RbacAuthTests`（13 项 OK）与全量测试套件（254 项 OK）

- [x] **a7 批次 A 全量回归**
  - [x] `makemigrations --check --dry-run`（No changes detected）& `migrate`（应用 `notifications/0005`）& `manage.py check`（无 issues）
  - [x] 5 个新模板编译校验（`get_template()` 全部 OK）
  - [x] `manage.py test` 全量通过（254 项，0 失败）
  - [x] 全表记录数基线核对（迁移前后 41 张表逐项比对，仅 `NotificationTemplate` 8→9，其余完全一致）
  - [x] 重启/热重载后实测 6 个新页面：`/expiry/calendar/`、`/expiry/export/`、`/daily_record/weekly_report/`、`/daily_record/annual_summary/` 经真实浏览器验证通过；`/operation_log/anomalies/`、`/system/health/` 因浏览器自动化环境超时未能取到浏览器证据，改以服务端渲染 200（48.9KB / 59.5KB 完整 HTML）+ 自动化测试断言覆盖
  - [x] 输出批次 A 验证报告（见 `checklist.md` 与最终答复，含决策点落地情况）

# Task Dependencies
- a7 依赖 a1~a6 与 a7-fix 全部完成
- a7-fix 依赖 a1 / a2 / a3 / a5 / a6 的视图与路由已落地
