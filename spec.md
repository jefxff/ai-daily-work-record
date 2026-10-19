# 批次 A 规格说明（到期预警 / 周报年报 / 任务告警 / 健康面板 / 审计异常）

项目根：`c:\Users\admin\Documents\trae_projects\ai-daily-work-record-0924\ai-daily-work-record-0525`

## 1. 背景与目标

用户提出 6 组增强设想。经评估确认**分批渐进**实施：每批做完 → 全量测试通过 → 再进入下一批。
本文件是**批次 A**的实施规格。批次 A 的硬约束：**零 DB schema 变更**（仅允许新增“数据迁移”用于播种通知模板）。

## 2. 范围 / 非目标

**范围内（批次 A，6 项）**
- A1 到期风暴日历（4 个到期概念 UNION）+「本月到期清单」Excel 导出
- A2 每日记录 → 周报生成（模板为主，AI 开关预留）
- A3 每日记录 → 年度个人总结卡片
- A4 定时任务连续失败 → 通知管理员
- A5 审计异常行为检测（**读取层**）
- A6 系统健康面板（只读：调度心跳 / 定时任务 / 备份历史 / 磁盘）

**明确非目标（推迟）**
- 批 B：`KnowledgeView` 阅读表 + 阅读报告；`TaskRunLog` 执行历史；`OperationLog.before_json/after_json`
- 批 C：请求量 / P95 / 慢查询采集中间件；外部独立探针
- A5 中的「导出行为专项追踪」：现有 `OperationLog` 的 `operation_type` 仅有 `新增/更新/删除/编辑`，无导出类型；为其补写点需改动约 15 个导出视图的写路径，风险高，推迟到批 B 与 before/after 一并做。

## 3. 关键既有事实（已核实，实施时直接使用）

- 视图工厂 [crud.py](file:///c:/Users/admin/Documents/trae_projects/ai-daily-work-record-0924/ai-daily-work-record-0525/records/crud.py)：`make_list_view` / `make_export_view`（openpyxl + `sanitize_xlsx_formulas` + `content_disposition`）/ `make_form_view` / `make_delete_view`。
  - **本批次新增导出不复用 `make_export_view`**：其契约是「单模型 + `queryset(request)`」，而到期清单是异构 UNION，故 A1 导出单独实现（仍复用 `work_record/export_utils`）。
- 权限 [decorators.py](file:///c:/Users/admin/Documents/trae_projects/ai-daily-work-record-0924/ai-daily-work-record-0525/accounts/decorators.py)：`@perm_required(module,action)`；跨模块页面无现成装饰器 → 视图内用 `has_perm(user,module,'read')` 自判并过滤来源。
- 模板标签 [accounts_tags.py](file:///c:/Users/admin/Documents/trae_projects/ai-daily-work-record-0924/ai-daily-work-record-0525/accounts/templatetags/accounts_tags.py)：`{% can_view %}`、`{% can "m" "a" %}`、`{% can_view_any "m1" "m2" %}` 均存在。
- 基础模板 [base.html](file:///c:/Users/admin/Documents/trae_projects/ai-daily-work-record-0924/ai-daily-work-record-0525/records/templates/records/base.html) blocks：`page_title / extra_css / content / top_nav_actions / extra_js`。`<script>` 内若含 Django 权限标签须加 `//` 前缀。
- 分页组件 [\_pagination.html](file:///c:/Users/admin/Documents/trae_projects/ai-daily-work-record-0924/ai-daily-work-record-0525/records/templates/records/_pagination.html)：`{% include 'records/_pagination.html' with page_obj=<Page对象> %}`。
- 侧栏 [\_sidebar.html](file:///c:/Users/admin/Documents/trae_projects/ai-daily-work-record-0924/ai-daily-work-record-0525/records/templates/records/_sidebar.html)：工作台组（`can_view_any ... insurance ...`，line 18–57）；档案组（`can_view "archive"`，line 81–101）；系统组（`can_view "system"`，line 103–119）。
- 通知服务 [services.py](file:///c:/Users/admin/Documents/trae_projects/ai-daily-work-record-0924/ai-daily-work-record-0525/notifications/services.py)：`emit(code, *, recipient, context, dedup_context, link, priority, ...)`；`users_with_perm(module, action)`；模板由数据迁移播种（[0002_seed_templates.py](file:///c:/Users/admin/Documents/trae_projects/ai-daily-work-record-0924/ai-daily-work-record-0525/notifications/migrations/0002_seed_templates.py)）。
- 调度 [scheduler.py](file:///c:/Users/admin/Documents/trae_projects/ai-daily-work-record-0924/ai-daily-work-record-0525/notifications/scheduler.py)：`HEARTBEATS`、`worker_alive()/scheduler_alive()`（90s 窗口）、`run_task(task, source)`（finally 写 `last_status/last_message/last_duration_ms/last_run_at`）、`next_run_at(task)`、`SCAN_INTERVAL_SECONDS=30`。
- 备份模型 [sysbackup/models.py](file:///c:/Users/admin/Documents/trae_projects/ai-daily-work-record-0924/ai-daily-work-record-0525/sysbackup/models.py)：`BackupOperationLog(op_type, operator, file_name, file_path, success, detail, created_at)`，`ordering=['-created_at']`；备份目录 `BASE_DIR/backups`，媒体目录 `settings.MEDIA_ROOT`。
- 到期数据源：`records.models.Insurance.end_date`（非空）；`archive.models.Archive.expire_date`（可空）；`archive.models.License.expire_date` 与 `License.next_review_date`（可空，`License` 用 `name/code`）。
- 每日记录 `DailyRecord(user, record_date=DateTimeField auto_now_add, department, work_content, due_date, status∈{完成,未完成}, progress 0-100)`；隔离入口 `_my_daily_records(user)`。
- `OperationLog(operation_time, operator, module_type, operation_type, object_identifier, change_summary)`，`operation_type` 取值 `新增/更新/删除/编辑`。
- 健康探测现有实现 [ai_api/views.py](file:///c:/Users/admin/Documents/trae_projects/ai-daily-work-record-0924/ai-daily-work-record-0525/ai_api/views.py#L1078-L1133) `health_check`（探测 DB / 迁移 / 日志目录，503 判定）。
- 路由：[work_record/urls.py](file:///c:/Users/admin/Documents/trae_projects/ai-daily-work-record-0924/ai-daily-work-record-0525/work_record/urls.py)（`''→records.urls`、`archive/`、`notifications/`、`system/backup/`）；records 路由在 [records/urls.py](file:///c:/Users/admin/Documents/trae_projects/ai-daily-work-record-0924/ai-daily-work-record-0525/records/urls.py)。

---

## 4. 详细规格

### A1 到期风暴日历 + 本月到期清单导出

**新增 `records/expiry.py`（服务层，纯读，无表）**
- 常量：
  - `SOURCE_META = {'insurance': {module:'insurance', kind_label:'保险'}, 'archive_contract': {module:'archive', kind_label:'合同'}, 'license_expire': {..., '证照有效期'}, 'license_review': {..., '证照年审'}}`
  - `DEFAULT_LEAD_DAYS = {'insurance':30, 'archive_contract':60, 'license_expire':60, 'license_review':30}`
  - `DEFAULT_HORIZON_DAYS = 90`，`LOOKBACK_DAYS = 90`（已过期回溯窗口，避免历史噪声）
  - `URGENT_DAYS = 7`（红/黄分界：≤7 天或已过期为红）
- `collect_expiry_items(user, *, horizon_days=90, lead_days=None, today=None) -> list[dict]`
  - 仅纳入用户有 `read` 权限的来源（`has_perm(user, module, 'read')`）。
  - 每来源查询窗口 `today - LOOKBACK_DAYS <= date <= today + horizon_days`：
    - insurance：`Insurance.objects.filter(end_date__range=(start,end))`
    - archive_contract：`Archive.objects.filter(expire_date__range=(start,end))`
    - license_expire：`License.objects.filter(expire_date__range=(start,end))`
    - license_review：`License.objects.filter(next_review_date__range=(start,end))`
  - 归一为 dict：`{kind, kind_label, title, subtitle, date, days_left, severity, lead_days, module, link}`。
    - `days_left = (date - today).days`
    - `severity`：`date < today` → `'danger'`（已过期）；`days_left <= URGENT_DAYS` → `'danger'`；`days_left <= lead_days` → `'warning'`；其余 → `'success'`。
    - `link`：保险 `/insurance/detail/<pk>/`；档案 `/archive/archives/<pk>/`；证照 `/archive/licenses/<pk>/`。
  - 返回按 `(date, kind)` 升序排序。
- `group_by_day(items)` → `[{'date':date, 'weekday':'周一', 'items':[...]}]`（升序）。
- `summarize(items)` → `{'expired':n,'urgent':n,'warning':n,'normal':n,'total':n}`。
- `month_items(items, year, month)` → 筛选 `date` 落在该自然月内（供导出）。

**新增 `records/views_expiry.py`**
- `expiry_calendar(request)`（GET）
  - 登录由 `LoginRequiredMiddleware` 保证；随后判定：若 `has_perm(user,'insurance','read') or has_perm(user,'archive','read')` 均否 → `render('accounts/403.html', {'required_module':'insurance/archive','required_action':'read'}, status=403)`。
  - 参数：`days`（clamp 30–365，默认 90）；`lead_insurance/lead_contract/lead_license`（clamp 1–180，分别映射 insurance/archive_contract(与 license_expire 共用合同默认→ 见决策点)/license）。**决策**：合同与证照有效期共用 `lead_contract`（二者同为“到期”语义），年审用 `lead_license`。为对齐用户“按类型分别配置”的诉求，另设 `lead_review`（默认 30）单独控制年审；即实际 4 个 lead：`lead_insurance/lead_contract/lead_license/lead_review`。
  - context：`days, today, horizon_end, by_day, summary, lead_days, sources_available, request`（供分页组件/GET 回填）。
  - 渲染 `records/templates/records/expiry_calendar.html`。
- `expiry_export(request)`（GET）
  - 同样权限判定（无任一 read → 403）。
  - 取**当月**（`today.year/month`）到期项（仅用户可读来源），生成 xlsx：
    - sheet 名 `本月到期清单`；表头 `['序号','类型','名称','说明','到期日期','剩余天数','状态','模块']`
    - `剩余天数` 负数表示已过期；`状态` 文案 已过期/紧急/临近/正常。
  - 空数据 → `HttpResponse('本月暂无到期事项')`。
  - 文件名 `本月到期清单-YYYYMMDD.xlsx`，`content_disposition`；写入前 `sanitize_xlsx_formulas(wb)`。
  - **不复用 `make_export_view`**（异构 UNION 不满足其单模型契约），实现保持同款防护。

**模板 `records/templates/records/expiry_calendar.html`**
- `{% extends 'records/base.html' %}`；`top_nav_actions` 放：窗口天数选择（GET `days`）+ 4 个提前天数输入 + 「导出本月到期清单」按钮。
- 内容：摘要条（已过期/紧急/临近/正常/合计，用 `.status-badge danger|warning|success`）+ 按天分组的卡片列表（每日一个 `.table-container`，日期头 + 事项行 + 严重度徽章 + 「查看」链接）。
- **决策**：日历形态采用「按天分组列表」而非月历网格（满足“按天排列”，实现简单、移动端友好）；月历网格列为后续可选增强。
- 仅用设计令牌（`var(--c-*)`、`var(--z-*)`），不写裸色值/内联样式。

**URL（records/urls.py）**
```python
from . import views_expiry
path('expiry/calendar/', views_expiry.expiry_calendar, name='expiry_calendar'),
path('expiry/export/',   views_expiry.expiry_export,   name='expiry_export'),
```
**侧栏**：在工作台组 `{% endcan_view_any %}`（line 57）之后、试样组之前插入：
```django
{% can_view_any "insurance" "archive" %}
<ul class="sidebar-menu-item">
  <li><a href="{% url 'expiry_calendar' %}" class="{% if '/expiry' in request.path %}active{% endif %}">
    <svg ...></svg><span>到期预警</span></a></li>
</ul>
{% endcan_view_any %}
```

---

### A2 每日记录 → 周报生成（模板为主 + 预留 AI 开关）

**新增 `records/views_reports.py`**
- `weekly_report(request)`（GET）
  - 数据隔离：`_my_daily_records(request.user)`，**仅本人**（决策：部门范围需跨用户读取，与“每日记录按用户隔离”硬约束冲突，本批不做）。
  - 参数 `start`（ISO 周首日，默认今天所在周的周一）；非法则回退默认。计算 `week_start/week_end`（周一~周日）。
  - 取当周记录；分三组：`完成`、`进行中`（`未完成` 且 `progress>0`）、`未完成`（`未完成` 且 `progress==0`）。
  - 生成 Markdown 文本（服务端字符串，供“一键复制”）：
    ```
    # 周报（YYYY-MM-DD ~ YYYY-MM-DD）
    本周共记录 N 项，完成 X 项、进行中 Y 项、未完成 Z 项。
    ## 已完成（X）
    1. [部门] 工作内容（进度 100%）
    ## 进行中（Y） ...
    ## 未完成（Z） ...
    ```
    每条 `work_content` 换行替换为空格并截断 200 字。
  - context：`week_start, week_end, prev_start, next_start, groups, stats, report_md, ai_enabled`。
  - 渲染 `records/templates/records/weekly_report.html`。
- `maybe_ai_polish(text)`：预留开关，`getattr(settings,'WEEKLY_REPORT_AI_ENABLED', False)` 为 False 时原样返回（本批仅占位，不接 AI）。

**模板 `records/templates/records/weekly_report.html`**
- `{% extends 'records/base.html' %}`；周切换（GET `start`，含 上一周/下一周/本周 链接）+ 统计卡 + `<pre id="reportMd">{{ report_md }}</pre>` + 「复制到剪贴板」按钮（`navigator.clipboard`，复用 `window.showToast`）。

**URL / 入口**
- `path('daily_record/weekly_report/', views_reports.weekly_report, name='weekly_report')`
- [daily_record_list.html](file:///c:/Users/admin/Documents/trae_projects/ai-daily-work-record-0924/ai-daily-work-record-0525/records/templates/records/daily_record_list.html) `top_nav_actions` 增加「生成周报」按钮。
- 侧栏：在 `{% can_view "daily_record" %}` 块（line 22–24）内新增 `<li>周报</li>`（同块两个 `li`）。

---

### A3 年度个人总结卡片

**`records/views_reports.py` 追加 `annual_summary(request)`**
- 参数 `year`（clamp 2000~今年，默认今年）；范围**仅本人**。
- 聚合（`_my_daily_records(user).filter(record_date__year=year)`）：
  - `total`、`done`（status='完成'）、`ongoing`、`avg_progress`
  - `departments`：`department` 去重计数 + Top 5
  - `max_streak`：最长连续记录天数（按 `record_date` 去重后的日期序列计算）
  - `active_months`：覆盖月份数（`TruncMonth` 去重）
  - `first_date/last_date`
- **不杜撰“参与项目数”**（`DailyRecord` 无项目字段），以“涉及部门数 / 覆盖月份数 / 连续记录天数”替代。
- 渲染 `records/templates/records/annual_summary.html`：一张可分享卡片（头像位用用户名首字），含「复制文本」按钮（与 A2 同法）。

**URL / 入口**
- `path('daily_record/annual_summary/', views_reports.annual_summary, name='annual_summary')`
- 侧栏 `{% can_view "daily_record" %}` 块内新增 `<li>年度总结</li>`。

---

### A4 定时任务连续失败告警

**新增数据迁移 `notifications/migrations/0005_seed_task_failure_template.py`**
（**实施修正**：原写 `0003` 系笔误——`0003_notification_batch_id` 与 `0004_scheduledtask` 已被占用，
故实际落盘为 `0005`，依赖 `0004_scheduledtask`。）
- 播种模板 `scheduled_task_failure`（category=`task`；title `定时任务连续失败`；content 含 `{{ task_name }}/{{ code }}/{{ fail_count }}/{{ last_message }}`；email 模板同款）。沿用 `0002` 的 `get_or_create(code=...)` 幂等写法。

**改 `notifications/scheduler.py`**
- 新增常量 `FAIL_STREAK_THRESHOLD = 3`、模块级进程内计数 `_FAIL_STREAK = {}`（**决策**：零 schema 约束下用进程内计数；进程/服务重启清零，语义为“本次运行内连续失败”，限制已记录在文档）。
- 新增 `_handle_task_result(task, ok, message)`（可单测）：
  - 成功 → `_FAIL_STREAK.pop(task.pk, None)`。
  - 失败 → `_FAIL_STREAK[task.pk] = _FAIL_STREAK.get(task.pk, 0) + 1`；当计数 `>= FAIL_STREAK_THRESHOLD` 时，向 `services.users_with_perm('system','update')` 逐个 `services.emit('scheduled_task_failure', recipient=..., context={task_name, code, fail_count, last_message}, dedup_context=f'task:{task.pk}:{today}', link='/system/health/')`；`link` 指向 A6 健康面板。
  - 发送失败仅记日志，不影响任务主流程（与既有留痕同款容错）。
- 在 `run_task()` 的 `finally` 写库之后调用 `_handle_task_result(task, ok, message)`。
- 函数内 `from . import services` 延迟导入，避免循环依赖。

---

### A6 系统健康面板（只读）

**新增 `work_record/health.py`**
- `probe_health() -> (checks: dict, healthy: bool)`：迁移现有 [ai_api/views.py](file:///c:/Users/admin/Documents/trae_projects/ai-daily-work-record-0924/ai-daily-work-record-0525/ai_api/views.py#L1078-L1133) 中 DB/迁移/日志目录三项探测，输出结构不变。
- 改 `ai_api/views.py:health_check` 改为调用 `probe_health()`，**payload 键与状态码保持完全一致**（杜绝回归）。

**新增 `notifications/views_health.py`**
- `system_health(request)`，装饰 `@perm_required('system','read')`：
  - `checks, healthy = probe_health()`
  - 调度心跳：`worker_alive/scheduler_alive` + `HEARTBEATS` 原始时间戳格式化。
  - 定时任务表：`ScheduledTask.objects.all()` → code/name/enabled/last_status/last_run_at/last_duration_ms/last_message(截断)/`next_run_at`。
  - 备份历史：`BackupOperationLog.objects.all()[:20]`（op_type/operator/file_name/success/created_at）。
  - 磁盘：`shutil.disk_usage(BASE_DIR)` 总/已用/可用/百分比；`db.sqlite3` 文件大小；`BASE_DIR/backups` 与 `MEDIA_ROOT` 目录总大小（`os.walk` + try/except 容错）。
  - 渲染 `notifications/templates/notifications/system_health.html`。
- **非目标提示区**：页面内明确标注 请求量/P95/慢查询 = 批 C 待做。

**URL（新增于 [work_record/urls.py](file:///c:/Users/admin/Documents/trae_projects/ai-daily-work-record-0924/ai-daily-work-record-0525/work_record/urls.py)）**
```python
from notifications import views_health
path('system/health/', views_health.system_health, name='system_health'),
```
（`system/backup/` 已由 sysbackup 占用；健康页独立挂在 `system/` 下，语义与「系统」侧栏组一致。）

**侧栏**：系统组（`can_view "system"`，line 106–118）内新增 `<li>系统健康</li>`（active 判 `/system/health`）。

---

### A5 审计异常行为检测（读取层）

**新增 `records/views_audit.py`**
- `audit_anomalies(request)`，装饰 `@perm_required('system','read')`：
  - 参数 `days`（clamp 1–90，默认 7）；`since = now - days`。
  - 检测项（全部基于现有 `OperationLog`，纯读）：
    1. **删除爆发**：`operation_type='删除'`，按 `(operator, TruncHour(operation_time))` 分组 `Count>20` → 命中列表。
    2. **非工作时间批量操作**：`ExtractHour(operation_time)` ∉ [8,18) 或 `ExtractIsoWeekDay` ∈ {6,7} 且 `operation_type in ('删除','编辑')`，按 `(operator, 日期)` 分组 `Count>=10` → 命中列表。
    3. **单小时高频操作**：按 `(operator, TruncHour)` 分组 `Count>50` → 命中列表。
  - context：`days, since, delete_bursts, off_hours, high_freq, empty_all`。
  - 渲染 `records/templates/records/audit_anomalies.html`（三段表格 + 窗口选择器；空态文案）。
- **决策**：「导出行为专项追踪」本批不做（现有日志无导出类型，需补写点），推迟至批 B。

**URL / 入口**
- `path('operation_log/anomalies/', views_audit.audit_anomalies, name='audit_anomalies')`
- [operation_log_list.html](file:///c:/Users/admin/Documents/trae_projects/ai-daily-work-record-0924/ai-daily-work-record-0525/records/templates/records/operation_log_list.html) `top_nav_actions` 增加「异常检测」按钮。
- 侧栏系统组「操作留痕」之后新增 `<li>异常检测</li>`。

---

## 5. 数据与安全约束（必须遵守）

1. 批次 A **不新增/修改任何 model 字段**；唯一迁移为 `notifications/0003`（播种模板，RunPython）。
2. 所有导出走 `sanitize_xlsx_formulas` + `content_disposition`。
3. 所有新页面遵循 RBAC；跨模块页（A1）在视图内按来源过滤权限。
4. 数据隔离：A2/A3 仅本人（`_my_daily_records`）；A5/A6/A4 面向 `system` 权限/超管。
5. 模板不得硬编码颜色/z-index；用 `var(--c-*)`/`var(--z-*)` 设计令牌。
6. `<script>` 内 Django 权限标签必须加 `//` 前缀。
7. 新增路由后**必须手动重启 Django 服务**（旧进程持旧路由表会 NoReverseMatch）。
8. 服务运行期间**禁止删除 `db.sqlite3-wal`/`-shm`**；禁止提交 `db.sqlite3`/`media/`/`.env`。

## 6. 验证计划（批次 A 收口）

1. `python manage.py makemigrations --check --dry-run` → 应报“无待生成迁移”（仅存在手写 0003 数据迁移）。
2. `python manage.py migrate` → 应用 `notifications/0003`。
3. `python manage.py check` → 无 error。
4. **模板编译校验**：脚本用 `django.template.loader.get_template()` 编译 5 个新模板，全部通过。
5. `python manage.py test` → 全量通过（基线约 238 项，新增 A1/A4/A5/A6 用例）。
6. **全表记录数基线核对**（关键）：
   - 预期变化仅：`NotificationTemplate` 8 → 9（新增 `scheduled_task_failure`）。
   - 其余业务表记录数与改动前**完全一致**；`OperationLog` 仅因正常操作追加。
   - 用脚本对比改动前后各业务表 `COUNT(*)`，输出差异清单。
7. **浏览器实测**（restart 后）：`/expiry/calendar/`、`/expiry/export/`、`/daily_record/weekly_report/`、`/daily_record/annual_summary/`、`/system/health/`、`/operation_log/anomalies/` —— 无控制台报错、布局/权限/侧栏入口正确、导出文件可打开。
8. 单元测试要点：
   - A1：无 insurance/archive 权限用户访问 → 403；仅 archive 权限时结果不含保险项；severity 分档正确。
   - A4：mock 命令连续失败 3 次 → 生成 `scheduled_task_failure` 通知（对超管）；成功一次后计数清零。
   - A5：构造同人 1 小时内 21 条删除 → 命中；正常量不命中。
   - A6：`system.read` 可访问、`system.read` 缺失 → 403；`probe_health` 与 `health_check` 输出一致。

## 7. 需用户确认的决策点（可在批准时一并覆盖）

| # | 决策 | 备选 |
|---|---|---|
| D1 | A1 日历用「按天分组列表」 | 月历网格（工作量更大，可后续加） |
| D2 | A2/A3 范围仅本人 | 部门范围（需跨用户读取，违背隔离约束，暂不做） |
| D3 | A4 连续失败计数用进程内计数（重启清零、零 schema） | 需迁移加 `fail_streak` 字段（超出批 A 约束） |
| D4 | A5 不含「导出行为追踪」 | 需改约 15 个导出视图写路径，推迟批 B |
| D5 | A6 不含请求量/P95/慢查询 | 需中间件采集，推迟批 C |
| D6 | 健康页 URL = `/system/health/` | 或并入 `/system/backup/health/`（语义较差） |
| D7 | 侧栏新增入口：到期预警 / 周报 / 年度总结 / 系统健康 / 异常检测 | 减少入口（仅按钮不挂侧栏） |
