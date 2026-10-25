# 全站技术约定（同一件事只允许一种做法）

本文件是**横切约定的单一出处**。新增模块时照此执行；发现偏离要么改代码、要么改这里
并说明理由，不要"两边都留着"。

来源：2026-10-25 跨模块一致性审计（`impl-consistency-audit-2026-10-25.md`）
逐项收口后的结果。

---

## 1. 操作留痕

| 事项 | 约定 |
|---|---|
| 写入入口 | 只用 `records.audit.log_operation()`（页面/服务）或 `write_operation()`（无 request 时）。**禁止** `OperationLog.objects.create()` |
| 模块名 / 动作名 | 从 `records.audit_vocab` 取（`MODULES` / `Action`）。写入时自动折叠同义词，未知词会记 warning |
| 操作人 | `operator` 存**展示名**（多数是用户名，档案/证照用姓名），`operator_username` 存账号。聚合统计一律按 `operator_username`（空的回退 `operator`） |
| 快照 | 新增传 `after`、删除传 `before`、编辑两者都传；导出/下载等无实体变更不传 |
| 查询 / 规则 | 用 `audit_vocab.expand_action()` / `expand_module()`，不要写裸字面量 `operation_type="编辑"` |
| 下拉选项 | `MODULE_CHOICES` / `ACTION_CHOICES`，不要硬编码列表 |
| 不加留痕的动作 | 高频个人动作（通知已读/全部已读）—— 会污染审计表 |

新增模块/动作时改 `records/audit_vocab.py` 一处即可，页面下拉与审计规则同步生效。

## 2. HTTP 响应

### 2.1 JSON 外壳

统一 `{"ok": bool, "message": str, ...}`：

```python
# 成功
JsonResponse({"ok": True, "message": "已保存"})
# 失败
JsonResponse({"ok": False, "message": "原因"}, status=422)
```

DRF 侧沿用 `error_payload()` 的形状（`ok/message/code/field_errors`）。
**禁止** `{"error": ...}` / `{"success": ...}` 这类第三种外壳。

### 2.2 状态码

| 码 | 用在什么情况 | 例 |
|---|---|---|
| 400 | **请求本身有问题**：缺参数、JSON 解析失败、参数格式/取值范围错、路径不在白名单 | 「请求体不是合法 JSON」「分页参数无效」 |
| 401 | 未登录 / 凭证失效 | 中间件与 `perm_required` 的匿名分支 |
| 403 | 已登录但无权限（含对象级） | RBAC、非超管操作超管账号 |
| 404 | 资源不存在 | `get_object_or_404` |
| 405 | 方法不允许 | 「仅支持 POST」 |
| 409 | 状态冲突（并发/重复） | 同位置档案已被占用 |
| 413 | 体积超限 | 导出行数超 `EXPORT_MAX_ROWS` |
| 422 | **请求合法但业务规则拒绝** | 两次密码不一致、供应商类型非法、恢复被维护模式拒绝以外的业务规则 |
| 429 | 限流 / 资源忙 | 导出并发槽位占满 |

判断口诀：**能不能改一改参数就通过？能 → 400；参数没问题但业务不允许 → 422。**

## 3. 权限

页面侧只有两个装饰器：`@perm_required(module, action)` 与
`@perm_required_any((m1,a1), (m2,a2))`。两者**已包含登录判定与 JSON/HTML 分支**，
不要再叠 `@login_required`（叠了会把匿名 AJAX 变成 302 HTML，而不是统一的 401 JSON）。

DRF 侧按场景选一种，不要混：

| 场景 | 写法 |
|---|---|
| 标准 ViewSet（模块内 CRUD） | 类属性 `perm_module = "xxx"` + `ModuleRbacPermission` |
| 有自定义 action | 同上加 `custom_action_perms`（缺失动作会被**合并**进标准映射，无需重复声明 list/update 等） |
| APIView / 跨模块 / 行级 | `perm_explicit_checks = True` + 视图内 `_require()`；行级过滤在 `get_object()` / `get_queryset()` |

对象级归属：`get_object()` 必须做归属或越权校验（不要只在个别 action 里查），
新增 action 时才不会漏。

## 4. 分页

页面列表一律 `_per_page(request)`（默认 10，可选 10/30/50），**不要写死条数**。
DRF 列表沿用 `_paginate()`（默认 20）。

## 5. 文件上传

文件头（magic byte）判定只有一份：`work_record.file_validation`。

```python
from work_record.file_validation import content_matches, normalize_ext, read_head
```

各模块保留自己的**白名单与大小上限**（业务差异：证明文件 20MB、档案附件 50MB、
导入表格 10MB），只把"文件内容是否与扩展名相符"委托给统一实现。
新增格式往 `MAGIC_PREFIXES` 加一行即可。

## 6. 导出

所有导出视图必须加 `@guarded_export`（并发闸门，占满返回 429），
并在构建工作簿前做行数上限检查（`max_export_rows()`，超限返回 413）。
中文文件名一律 `content_disposition()`；工作簿一律 `sanitize_xlsx_formulas()`。

## 7. 时间

入库/比较用 `timezone.now()` / `timezone.localdate()`；展示用 `timezone.localtime()`。
**禁止** `datetime.now()` / `date.today()`（`USE_TZ=True` 下会引入 naive 时间）。

## 8. 日志

模块级 `logger = logging.getLogger(__name__)`。禁止在函数体内 `import logging`
（`knowledge_base/views.py` 曾有 13 处，已收口）。

## 9. 事务

多步写入用 `transaction.atomic()`；副作用（发通知、写文件）用 `transaction.on_commit()`
包一层，避免回滚后仍执行。`select_for_update()` 仅在确有并发争用处使用。
