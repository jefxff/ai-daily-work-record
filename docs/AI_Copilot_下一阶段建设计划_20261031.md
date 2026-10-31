# AI 助手（Copilot）下一阶段建设计划书

> 版本：v1.0 ｜ 日期：2026-10-31 ｜ 基线：`main@7f71b1e`
> 适用范围：内部单机部署（Django 5.2 + SQLite + 内网），面向 51 个账号的日常业务查询
> 依据：`docs/AI_Copilot_开发方案_20261031.md`（§1~§28）、`copilot/` 现有实现、真实库统计

---

## 0. 摘要

AI 助手已经跨过"能不能用"这条线：21 个业务模块接入、303 条问句矩阵 100% 通过、230 例测试全绿、
外发分级与审计落库。但它现在的能力形态是**"一个能答对的查询框"**，离"团队每天主动打开的工具"
还有三步：**可信（错了能被发现并修正）、好用（不用等、不用重复问、能带走结果）、主动（数据来找人）**。

下一步建议按三批推进，每批 2 周，共 6 周、约 44 人日（按 1.5 人并行估算，见 §8）：

| 批次 | 主题 | 核心交付 | 决策理由 |
| --- | --- | --- | --- |
| **B1** | 可信 | 反馈闭环、会话管理、模型链路金样集、引用与权限一致性 | 两张表（`CopilotFeedback`/`CopilotEvalCase`）已建好但没有入口，等于质量体系只有一半；会话保留策略写在策略表里却没人执行，是**承诺未落地** |
| **B2** | 好用 | 流式输出、多轮跟问、回答沉淀与导出、场景入口 | 当前回答要等整段返回（轮询 800ms），且只能在面板里看、复制不走；用户会退回到"自己翻列表" |
| **B3** | 主动 | 每日简报、主动提醒、质量成本看板、检索升级 | 项目已有进程内调度器、通知渠道、指标中间件，做"主动"是**接线**而不是造轮子 |

**四个明确不做**（见 §3.3）：多租户/权限体系重构、独立向量数据库、开放域闲聊、AI 直接改业务数据。

---

## 1. 现状盘点

### 1.1 已建成能力（按处理链路分层）

| 层 | 能力 | 实现位置 | 成熟度 |
| --- | --- | --- | --- |
| 接入 | 21 个模块统一描述（字段/状态/分组/统计/权限/软删） | `copilot/catalog.py`（775 行） | 稳定 |
| 理解 | 模块识别、状态识别、统计识别、排序/极值、求和、纯实体检索、算式/日期/闲聊本地直答 | `copilot/engine/planner.py`（1 380 行） | 稳定，靠 303 条矩阵兜底 |
| 执行 | 8 个工具：`list_modules`/`schema_of_module`/`query_records`/`get_record`/`aggregate_records`/`global_search`/`search_knowledge`/`list_attachments` | `copilot/tools/` | 稳定 |
| 模型 | 3 类 provider（MiniMax / Ollama / OpenAI 兼容），原生工具调用与 JSON 协议双模 | `copilot/providers/` | 稳定 |
| 安全 | L1~L4 分级、云端逐模块放行、字段级脱敏、限流、外发审计 | `copilot/security.py` | 稳定 |
| 呈现 | Markdown 服务端渲染 + bleach 白名单、可拖动/钉住面板、步骤与引用 | `copilot/markdown_render.py`、`static/copilot/` | 稳定 |
| 质量 | 303 条问句矩阵、230 例单测、`copilot_eval` / `copilot_grant` / `copilot_reap` | `copilot/eval_matrix.py`、`copilot/tests/` | **偏科**（只覆盖本地链路） |
| 运维 | 线程池 + 心跳 + 超时回收 + 进程重启中断标记 | `copilot/tasks.py` | 可用 |

### 1.2 量化基线（真实库与真实运行数据）

| 指标 | 当前值 | 来源 |
| --- | --- | --- |
| 接入模块数 | 21 | `accounts/registry.MODULES` ∩ `catalog.MODULE_MAP` |
| 问句矩阵 | 303 条（8 类问法覆盖 21 个模块，各模块 11~19 条）100% 通过 | `manage.py copilot_eval` |
| 单元测试 | 230 例全过 | `manage.py test copilot` |
| 真实会话 | 16 个会话 / 60 次运行 / 119 条消息 | 生产库 `copilot_copilot*` |
| 失败运行 | 1 次（超时/失败/中断合计） | 同上 |
| 知识库可检索文档 | 50 篇（已发布 31 篇） | `knowledge_base` |
| 数据体量 | SQLite 2.8 MB，最大表 `RequestMetric` 3 328 行 | `db.sqlite3` |
| 并发模型 | 进程内线程池 `max_workers=2`，SQLite 单写锁 | `copilot/tasks.py`、settings |
| **本地应答率（实测）** | **42%**（25/60 次运行未调用模型，无 token 消耗） | `CopilotRun.total_tokens=0` |
| 平均 token/次 | 3 872（模型链路，含 3 轮工具调用） | `CopilotRun.total_tokens` 均值 |
| 工具调用 | 77 次 / 被拒 7 次（9%），其中 6 次是 `group_by`/`order_by` 猜了不存在的字段 | `CopilotToolCall` |
| 外发拦截 | 3 次（`EGRESS_BLOCKED`） | `CopilotRun.error_code` |
| **时延（实测）** | 本地直答中位 **0.03s** / P95 0.07s；模型链路中位 **4.0s** / P95 **5.8s**；最长 122s（超时那条） | `CopilotRun` 完成时间 − 创建时间 |

### 1.3 可直接复用的既有基础设施（关键）

这一节决定了后续成本：**"主动"和"治理"两批不需要新造轮子**。

| 既有设施 | 位置 | 可复用到 AI 场景的方式 |
| --- | --- | --- |
| 进程内调度器 + 任务表 + 执行历史 | `notifications/scheduler.py`、`ScheduledTask`、`TaskRunLog` | 新增 `manage.py copilot_daily_digest` 等命令，登记为 `ScheduledTask` 即自动调度、自动留痕 |
| 通知渠道（站内 + 邮件） | `notifications/channels.py`、`services.py`、`templates` | 简报/提醒走模板 + `InAppChannel`/`EmailChannel`，无需自建推送 |
| 请求指标与健康面板 | `notifications/metrics.py`、`RequestMetric`、`notifications/views_health.py` | AI 用量看板照抄这套采集与展示风格 |
| 审计留痕与词汇 | `records/audit_vocab.py`、`OperationLog` | 已补 `AI 问答`/`AI 工具调用`/`AI 外发数据` 等动作词，续做沉淀类功能直接复用 |
| 导出与公式注入防护 | `work_record/export_utils.py`、`records/crud.make_export_view` | B2 的"导出 Excel"直接复用，不另写一套 |

---

## 2. 问题诊断

判定口径：**P0** = 影响结论正确性或合规承诺；**P1** = 明显影响日常使用效率；**P2** = 治理与演进。

### 2.1 质量与可信

| # | 问题 | 证据 | 影响 | 级别 |
| --- | --- | --- | --- | --- |
| Q1 | **模型链路没有回归基线** | `eval_matrix.py` 首行注明"LLM 全程不参与"；303 条只测本地链路。实测 77 次工具调用中 7 次参数被拒，6 次是猜错 `group_by`/`order_by` 字段名 | 长尾问题（跨模块推理、模糊指代）全走模型，答错无人知晓；参数错误还会多耗一轮 token | P0 |
| Q2 | **反馈闭环半成品** | `CopilotFeedback` 表、`/copilot/feedback/` 接口、admin 只读页都在；`panel.js` 没有任何 feedback 调用 | 用户答错了只能忍着或来提需求；错例无法沉淀成用例，同类问题反复出现 | P0 |
| Q3 | **知识库检索是纯 `icontains`** | `tools/search.py:174` `title/content/summary/tags__icontains` | 50 篇时够用，词不达意就漏；文档破百后召回明显下降 | P2 |

### 2.2 体验与效率

| # | 问题 | 证据 | 影响 | 级别 |
| --- | --- | --- | --- | --- |
| E1 | **没有会话列表** | 前端只调 `/conversation/latest/`，`/conversation/<pk>/` 接口无人调用 | 只能看"最近一次"，昨天的分析翻不回来；"新对话"一点旧内容就没了 | P0 |
| E2 | **回答要等整段返回** | 前端每 800ms 轮询 `/run/<id>/`，未完成只显示"正在处理…"。实测模型链路中位 4.0s、P95 5.8s，最长 122s，全程零反馈 | 查询不是慢，是"看起来卡死"；本地直答只要 0.03s，反差让等待更难忍 | P1 |
| E3 | **多轮跟问会失忆** | `planner.MAX_HISTORY=4`、`HISTORY_CHAR_LIMIT=600` | "那上面那个客户呢"超过两轮就断片，被迫重述条件 | P1 |
| E4 | **结果带不走** | `panel.js` 无复制/导出（`grep copy` 命中 0） | 用户要贴到周报里，只能手抄；也无法回到列表页核对 | P1 |
| E5 | **只能在面板里发问** | 无列表页/详情页入口 | 看到某客户想追问，得自己复制名字再敲一遍 | P2 |

### 2.3 安全与合规

| # | 问题 | 证据 | 影响 | 级别 |
| --- | --- | --- | --- | --- |
| S1 | **保留策略未执行** | `CopilotPolicy.retention_days=90`，但无任何清理任务（命令只有 eval/grant/reap） | 会话与消息只增不减，等于"承诺 90 天"没兑现 | P0 |
| S2 | **历史回答不按当前权限复核** | `views._conversation_data` 直接把消息返回 | 权限被收回后，历史回答里的该模块数据仍可见 | P1 |
| S3 | **外发审计无查询界面** | `CopilotEgressLog` 仅在 Django admin，`ReadOnlyAdmin` | 合规问询"谁把什么发给了哪家模型"要开发来查库 | P2 |
| S4 | 脱敏按**字段名**匹配 | `security._rule_for`（`*_phone`/`*_email`/`id_card`） | 客户名、地址、金额这类"值本身敏感"的字段不脱敏；属**已知取舍**，非缺陷 | P2（需明确写进合规说明） |

### 2.4 运维与成本

| # | 问题 | 证据 | 影响 | 级别 |
| --- | --- | --- | --- | --- |
| O1 | **无 AI 用量与成本视图** | token 记在 `CopilotMessage/CopilotRun`，但无页面 | 不知道每月烧多少 token、哪个模块最贵、被外发策略挡了多少次 | P2 |
| O2 | 线程池无监控 | `COPILOT_MAX_WORKERS=2`，队列积压不可见 | 多人同时问时排队，用户只看到"正在处理…" | P2 |

### 2.5 优先级矩阵

| 级别 | 项 | 归属批次 |
| --- | --- | --- |
| P0 | Q1 模型金样集、Q2 反馈闭环、E1 会话管理、S1 保留策略 | B1 |
| P1 | E2 流式、E3 多轮跟问、E4 沉淀导出、S2 权限复核 | B2（S2 提前到 B1-4 收尾） |
| P2 | E5 场景入口、Q3 检索升级、S3 外发界面、O1 看板、O2 队列可见 | B3 |

---

## 3. 目标、原则与非目标

### 3.1 阶段目标（6 周后应能验证的三件事）

1. **错例能被发现并修掉**：任一条错答可一键反馈，一周内由 `copilot_promote` 转成评估用例草稿，人工确认后纳入回归；
2. **日常查询不用等**：本地链路 P95 ≤ 0.5s（含网络往返，当前 0.07s），模型链路首字 ≤ 1.5s、完整 P95 ≤ 8s（当前 5.8s，不倒退）；
3. **数据能找人**：每个工作日 9:00 自动推送一条"今日需要关注"的简报（到期、逾期、异常），点开即问答。

### 3.2 设计原则（延续既有约束，不推翻）

- **本地优先**：能确定性算出来的，绝不交给模型（当前本地直答占 42%，目标 60%+）；
- **可解释**：每个结论都要能点到来源记录（`refs` 已有，B2 补齐"从结论跳列表"）；
- **最小外发**：默认本地模型；云端逐模块放行；脱敏在返回值上做（模型看不到原始值）；
- **渐进交付**：每批独立可上线、可回滚；每批结束跑 `copilot_eval` + `test copilot` + 全量测试；
- **不引入新中间件**：SQLite + 单机 + 进程内调度的部署形态不变（数据量 2.8MB，远未到换库阈值）。

### 3.3 非目标（本期明确不做）

| 非目标 | 理由 |
| --- | --- |
| 多租户 / 权限体系重构 | 现网 51 账号、内网单组织，RBAC 已够；重构风险远大于收益 |
| 独立向量数据库 / 外部检索服务 | 知识库 50 篇、总数据 2.8MB；先做分词与 BM25 排序即可，向量化等到文档 >500 篇再评估 |
| 开放域闲聊与常识问答 | 站内助手定位是"查业务"，越界回答会消耗信任；问能力/问候已由 `instant_reply` 本地兜底 |
| 自动写业务数据（AI 直接改记录） | 审计与责任边界不清；B2 只做"生成草稿供人工确认"，落库仍需人点 |

---

## 4. 路线图

> 每项按「为什么 → 怎么做 → 改动点 → 验收标准 → 风险」展开。验收标准必须可执行（命令或可观察行为）。

### 批次 B1：可信（第 1~2 周）

#### B1-1 反馈闭环（Q2）

- **为什么**：表、接口、admin 都有，唯独没有入口——这是全套质量体系里最便宜的一块拼图。
- **怎么做**：
  1. 面板每条助手回答底部加「有用 / 没用」两个图标按钮（沿用标题栏按钮样式），点击调 `/copilot/feedback/`；
  2. 点「没用」弹一行可选原因（答错模块 / 数字不对 / 没有这条 / 太慢），写入 `CopilotFeedback.comment`；
  3. 新增管理命令 `copilot_promote`：把 7 天内 `vote=-1` 的反馈，连同问题、实际回答、涉及模块，生成 `CopilotEvalCase` 草稿（`active=False`），管理员在 admin 勾选启用；
  4. 每周一汇总：`copilot_feedback_report` 输出不满意率 Top 10 问题，写入 `TaskRunLog` 并在健康面板展示。
- **改动点**：`copilot/views.py`（feedback 已存在，补 comment/reason 校验）、`static/copilot/panel.js`、`panel.css`、`copilot/management/commands/copilot_promote.py`、`copilot_feedback_report.py`（均新增）、`copilot/models.py`（`CopilotEvalCase` 加 `source_feedback` 外键，可空）。
- **验收**：
  - 面板可点赞/踩，刷新后状态保留（按 `message+user` 唯一键）；
  - `manage.py copilot_promote --dry-run` 能把 1 条真实踩的反馈转成用例草稿；
  - 新增 3 例测试（重复投票幂等、越权投票 403、草稿字段完整）。
- **风险**：反馈太稀疏 → 不追求覆盖率，只作为"错例来源"，不做满意度 KPI。

#### B1-2 会话管理 + 保留策略执行（E1、S1）

- **为什么**：只能看最近一次，等于没有历史；而 `retention_days=90` 无人执行，是合规承诺缺口。
- **怎么做**：
  1. 面板标题栏加「历史」按钮，抽屉列出最近 20 个会话（标题=首问截断 60 字 + 最后活跃时间 + 消息数），点击加载 `/conversation/<pk>/`；
  2. 会话项支持「重命名 / 删除」，删除走软删（`status=archived`）+ 30 天后物理清理；
  3. 新增 `manage.py copilot_prune`：按 `CopilotPolicy.retention_days` 清理过期会话、孤立 run、`CopilotToolCall` 与 `CopilotEgressLog`（外发日志单独保留 180 天，不随会话删）；
  4. 登记为 `ScheduledTask`（每日 03:30），执行结果进 `TaskRunLog`。
- **改动点**：`copilot/views.py`（新增 `conversations` 列表、`rename`、`delete`）、`copilot/urls.py`、`panel.js`/`panel.css`、`copilot/management/commands/copilot_prune.py`（新增）、`CopilotPolicy` 增加 `egress_retention_days`。
- **验收**：
  - 能切到 3 天前的会话并继续追问（上下文正确带入）；
  - `copilot_prune --dry-run` 输出将删除条数；实跑后过期数据为 0；
  - 删除会话后 `history` 接口 404，`latest` 顺延到上一个。
- **风险**：误删 → 全部走 `--dry-run` 先看数量，物理删除前二次确认参数 `--confirm`。

#### B1-3 模型链路金样集（Q1）

- **为什么**：303 条矩阵证明"能确定性回答的都对了"，但**长尾走模型的部分完全没有基线**——这正是用户体感"有时聪明有时离谱"的来源。
- **怎么做**：
  1. 从现有 `question_bank` + 真实会话里挑 60 条必须走模型的问法（跨模块关联、模糊指代、需要比较），写成 `CopilotEvalCase`；
  2. `copilot_eval` 增加 `--with-model` 模式：真实调用一次（受 `max_tool_rounds` 限制），用三重判定——是否调对工具、数字是否与库一致、是否包含必须出现/禁止出现的关键词；
  3. 输出基线报告：通过率、平均轮次、平均 token、失败归类（识别错/数字错/答非所问/越权被拒）；
  4. 把报告落盘到 `docs/eval/` 并标注模型名与日期，换模型时对比。
- **改动点**：`copilot/eval_matrix.py`（新增 `ModelCase` 与判定）、`management/commands/copilot_eval.py`（新参数）、`docs/AI_Copilot_评估报告_20261031.md`（追加模型链路章节）。
- **验收**：
  - `manage.py copilot_eval --with-model` 能跑通并产出报告；
  - 60 条基线通过率 ≥ 85%（首测基线允许更低，但必须记录真实值）；
  - 至少 3 条已知错例被复现，且修复后转绿（形成"错例→回归"的闭环）。
- **风险**：模型不稳定导致结果抖动 → 每条跑 2 次取"两次都过才算过"，并在报告里注明抖动率。

#### B1-4 引用与权限一致性（S2）

- **为什么**：会话是自己的，但"当时有权限、现在没有"的数据不该继续出现在历史回答里。
- **怎么做**：
  1. `_conversation_data` 按**当前**权限过滤：回答里引用了无权限模块的 `refs`，整条消息返回时标注"该回答涉及你已无权访问的模块"并隐去正文；
  2. `refs` 里的链接加"模块级"权限校验，点进去由目标页自身 403 兜底（已有 `@perm_required`，无需重复）；
  3. 审计：权限变更（`accounts` 角色调整）后，不主动清历史，只在读取时判定——避免大批量写。
- **改动点**：`copilot/views.py`（`_conversation_data`）、`copilot/registry.py`（复用 `available_modules`）。
- **验收**：撤掉某模块 read 权限后，历史回答正文被隐去、`refs` 不返回该模块；恢复权限后原样可见；新增 2 例测试。

### 批次 B2：好用（第 3~4 周）

#### B2-1 流式输出（E2）

- **为什么**：轮询模式下，模型链路那 4~6 秒（P95 5.8s，最长 122s）里界面只有"正在处理…"，慢是次要的，**没反馈**才是主要感受。
- **怎么做**：
  1. 新增 `GET /copilot/run/<id>/stream/`（`StreamingHttpResponse`，`text/event-stream`），逐块推送 `token` / `step` / `done` 事件；
  2. 执行器侧把 provider 的增量回调透传到 `run.events` 队列（进程内 `queue.Queue`，单进程部署可行；不引入 Redis）；
  3. 前端优先用 `EventSource`，失败自动降级回现有轮询（保留 `/run/<id>/` 接口不动，保证向后兼容）；
  4. 本地链路（本地直答）本来就快，无需流式，仍走一次性返回。
- **改动点**：`copilot/views.py`、`copilot/urls.py`、`copilot/engine/executor.py`、`copilot/providers/*`（增量回调）、`panel.js`。
- **验收**：模型链路首字 ≤ 1.5s（本地模型）、P95 完整 ≤ 8s（与 §5 口径一致）；断线重连后能拿到已完成答案；轮询降级路径仍可用（旧浏览器）。
- **风险**：`StreamingHttpResponse` 与 SQLite 长事务冲突 → 流式期间不持有事务，只在结束时一次写库（沿用现有 `_finish`）。

#### B2-2 多轮跟问与指代消解（E3）

- **为什么**：`MAX_HISTORY=4`、每条 600 字，用户说"那这个客户呢"必然断片，只能重复描述条件。
- **怎么做**：
  1. 会话级"**槽位记忆**"：把上一轮解析出的模块、关键词、状态、时间范围存进 `conversation.slots`（JSON），追问时先看槽位再看正文；
  2. 指代词（这个/那个/上面/刚才）在本地链路直接替换为槽位值，再走原有识别——不依赖模型猜；
  3. 历史消息窗口改为"**摘要 + 最近 2 轮原文**"，摘要由本地模板生成（模块/条件/结论一句话），不额外调模型。
- **改动点**：`copilot/engine/planner.py`（槽位读写、指代替换）、`copilot/models.py`（`CopilotConversation.slots`）、`executor.py`（写入槽位）。
- **验收**：连续追问"这个客户有哪些客诉""那上个月的记录呢"能在 3 轮内正确继承条件；新增 6 例问句进 `question_bank` 与矩阵。

#### B2-3 回答沉淀与导出（E4）

- **为什么**：查到结果后，用户的下一步动作是"贴进周报"或"发给同事"，现在只能手抄。
- **怎么做**：
  1. 每条回答右上角「复制」（Markdown 原文，复用 `markdown_render.to_plain` 的表格制表符分隔逻辑）；
  2. 「导出 Excel」：把本次回答的结构化行（`refs` + 查询结果）交给 `work_record/export_utils`，带表头、来源列与生成时间，沿用公式注入防护；
  3. 「存为知识库草稿」：一键把问答（问题+结论+来源链接）生成 `KnowledgeDocument` 草稿（`status=draft`，`owner=当前用户`），需人工编辑后发布——对应审计词表里的 `AI 生成草稿`。
- **改动点**：`copilot/views.py`（`export`、`save_draft` 接口）、`copilot/urls.py`、`panel.js`、`work_record/export_utils.py`（复用）。
- **验收**：导出文件能被 Excel 正常打开、公式单元格以文本呈现；草稿进入知识库待发布列表；各 2 例测试。

#### B2-4 业务场景入口（E5）

- **为什么**：真实场景是"我在客户列表看到这一行，想立刻追问"，而不是打开面板重新描述。
- **怎么做**：
  1. 列表页表格操作列加「问 AI」（沿用 `table-tools.js` 的操作列收纳逻辑），携带当前行关键字段生成预填问题；
  2. 详情页右上角同样入口，预填问题带上记录编号；
  3. 预填而非自动发送：用户可改条件后再发，避免"AI 擅自替他决定查什么"。
- **改动点**：`static/js/table-tools.js`、列表模板（`records/templates/...`）、`panel.js`（`openWith(question)` 暴露为 `window.copilotAsk`）。
- **验收**：在客户台账点行内「问 AI」→ 面板打开且问题已填；点击不触发整行跳转（事件冒泡处理）。

#### B2-5 窄屏与移动端（延续 E1）

- **为什么**：面板 400px 固定宽，手机上是"能看但不好用"；外勤查档案/证照是真实场景。
- **怎么做**：≤600px 时面板改为全屏抽屉（上下留 8px），标题栏加「返回」；表格维持横向滚动；触控目标 ≥ 40px。
- **验收**：iPhone SE 尺寸（375×667）下面板不溢出、可拖动/钉住逻辑自动禁用（全屏即固定）。

### 批次 B3：主动（第 5~6 周）

#### B3-1 每日业务简报（新能力，复用调度器）

- **为什么**："数据找人"是站内助手价值跃升的关键；项目已有调度器与通知渠道，成本主要在内容组装。
- **怎么做**：
  1. 新增 `manage.py copilot_daily_digest`：汇总当日到期/逾期（复用 `records/expiry.py`）、未完成日报、新增客诉、借阅超期，用本地模板生成 5~8 行摘要；
  2. 生成后写入 `CopilotConversation`（系统会话），用户点通知直接进入对话看详情并可追问；
  3. 登记 `ScheduledTask`（工作日 09:00），渠道走站内 + 邮件（沿用 `EmailChannel`）。
     *落地定案（2026-10-31）：渠道仅站内，不加邮件，见 §10 渠道决策。*
- **验收**：连续 3 个工作日按时送达；简报数字与列表页一致（写 1 例测试比对 `expiry.collect_expiry_items`）。

#### B3-2 主动提醒与异常发现（规则优先，模型只写话术）

- **为什么**：用户现在要"想到才问"；异常（连续多天无日报、同一客户客诉激增）更适合主动推。
- **怎么做**：定义 3 条确定性规则（不靠模型判断），命中后由模板生成提醒，模型仅负责润色措辞（可关闭）。
- **验收**：规则命中可在 admin 复现；误报可一键"不再提醒"（写 `ScheduledTask` 参数）。

#### B3-3 AI 质量与成本看板（O1、O2）

- **为什么**：没有度量就没有优化方向；现在连"每月多少 token"都答不出。
- **怎么做**：新增只读页面（沿用健康面板风格）：本地应答率、模型调用次数、token 与估算成本、P50/P95 时延、失败码分布、外发拦截次数、Top 提问模块、反馈不满意率、线程池队列长度。
- **验收**：页面数据与 `CopilotRun` 手写 SQL 统计一致（写 1 例测试）；支持按周筛选。

#### B3-4 检索升级与模型路由（Q3、S3）

- **为什么**：知识库到 100 篇以上时 `icontains` 会明显漏召回；同时"润色用小模型、问答用大模型"的 `purpose` 机制已就绪，但路由用途尚未落地。
- **怎么做**：
  1. 检索：中文分词（`jieba` 已有依赖则复用；否则用现有 `split_terms` + 词频加权）+ BM25 排序，先不上向量；
  2. 路由：把"是否需要模型"的判断交给 `purpose=router` 的小模型（阈值可配），本地链路命中率越低越依赖它；
  3. 外发审计查询页：按时间/用户/模块筛选 `CopilotEgressLog`，超管可见。
- **验收**：知识库检索 Top5 命中率在 30 条人工标注集上 ≥ 80%；路由开启后本地应答率下降不超过 5 个百分点。

---

## 5. 度量体系

**北极星指标：本地一次答对率**——不追问、不纠正、不点"没用"的首次回答占比（周维度）。

| 类型 | 指标 | 定义/取数 | 基线 | 6 周目标 |
| --- | --- | --- | --- | --- |
| 主 | 本地一次答对率 | （总问答 − 追问 − 点踩 − 重问）÷ 总问答 | 未度量（先补埋点） | ≥ 75% |
| 主 | 本地应答率 | 未调用模型的问答 ÷ 总问答 | **42%**（25/60） | ≥ 60% |
| 质量 | 矩阵通过率 | `copilot_eval` | 303/303 | 保持 100% |
| 质量 | 模型金样通过率 | `copilot_eval --with-model` | 待测 | ≥ 85% |
| 体验 | 首字时延 P95 | 前端埋点 → `CopilotMessage.latency_ms` | 无流式，首字即完整（P95 5.8s） | ≤ 1.5s |
| 体验 | 完整时延 P95 | 同上 | **5.8s**（模型链路；本地直答 0.07s） | ≤ 8s |
| 成本 | 单次问答 token | `CopilotRun.total_tokens` 均值 | **3 872** | 下降 20% |
| 安全 | 外发拦截命中 | `steps.tool=egress_blocked` 计数 | **3 次** | 保持 > 0（策略生效） |
| 安全 | 工具参数越界拦截 | `CopilotToolCall.permitted=False` | 7 次 / 77（9%） | 降至 5% 以下 |
| 稳定 | 失败率 | failed+timeout+interrupted ÷ 总数 | **1/60 ≈ 1.7%** | ≤ 1% |
| 采纳 | 7 日回访率 | 7 天内再次使用的账号 ÷ 活跃账号 | 样本不足（16 会话集中在 1 天） | ≥ 50% |

> 说明：唯一仍无基线的是"本地一次答对率"——它依赖反馈埋点（B1-1），先补埋点再定目标；其余基线均取自真实库，可直接复算（见 §9.1）。

---

## 6. 交付规范与发布

| 环节 | 要求 |
| --- | --- |
| 代码 | 每项一个 PR/提交，提交信息写清楚"问题—方案—验收"；涉及前端资源必须同时 `collectstatic` 并升 `?v=` |
| 测试 | 新增功能必须带测试；每批结束跑 `manage.py test copilot`、`manage.py copilot_eval`、全量 `manage.py test`（当前已知红：`records.tests_expiry.MonthCalendarTests` 月末边界，非本期引入） |
| 数据 | 新增字段一律可空或带默认值；迁移只做加法；`copilot_prune` 等破坏性命令必须支持 `--dry-run` |
| 上线 | 先在本机 8999 验证 → 备份库 → 迁移 → `collectstatic` → 重启 → `python scripts/smoke_copilot.py` 自检 |
| 回滚 | 代码回滚 + 迁移回滚脚本（本期迁移均为加字段，可安全 `migrate <app> <prev>`）；前端带 `?v=` 可秒回旧版 |
| 文档 | 每批更新 `docs/AI_Copilot_开发方案_20261031.md` 追加章节；度量看板截图入 `docs/` |

---

## 7. 风险登记册

| 风险 | 概率 | 影响 | 应对 |
| --- | --- | --- | --- |
| 模型链路金样集"标准答案"本身有争议 | 中 | 高 | 每条用例只断言"可机械验证"的部分（工具、数字、禁词），主观质量靠人看报告 |
| 流式改造引入并发/事务问题 | 中 | 高 | 流式只读不写；结束时一次落库；保留轮询降级路径作为兜底 |
| 反馈量太少，闭环形同虚设 | 高 | 中 | 不追求量；把"点踩"入口做轻（一次点击），并接受"每周 3~5 条"即为有效 |
| 每日简报被当成噪声 | 中 | 中 | 首版只推"今天必须处理"的 3~5 条；提供"暂停一周"开关 |
| 上下文槽位记忆导致串话（张冠李戴） | 中 | 高 | 槽位只在"明确追问"时启用（含指代词/省略主语），换话题自动清空；写 6 例反例测试 |
| SQLite 写竞争随并发上升 | 低 | 中 | 单进程 + 2 worker 已够；看板监控 `database is locked` 计数，超阈值再评估 WAL/换库 |
| 知识库分词引入新依赖 | 低 | 低 | 优先复用现有 `split_terms`；新增依赖需过 `requirements.lock` 与安全扫描 |

---

## 8. 排期与人力

按 1 名后端 + 1 名前端（或全栈 1.5 人）估算：

| 批次 | 周次 | 主要项 | 人日 | 关键依赖 |
| --- | --- | --- | --- | --- |
| B1 | 第 1 周 | B1-1 反馈闭环（3）、B1-2 会话管理（4） | 7 | 无 |
| B1 | 第 2 周 | B1-3 模型金样集（4）、B1-4 权限一致性（2）、回归（1） | 7 | 需可用的本地模型 |
| B2 | 第 3 周 | B2-1 流式（5）、B2-5 窄屏（2） | 7 | 流式依赖 provider 增量回调 |
| B2 | 第 4 周 | B2-2 多轮跟问（3）、B2-3 沉淀导出（3）、B2-4 场景入口（2） | 8 | 导出复用 `export_utils` |
| B3 | 第 5 周 | B3-1 每日简报（4）、B3-2 主动提醒（3） | 7 | 复用调度器与通知渠道 |
| B3 | 第 6 周 | B3-3 看板（3）、B3-4 检索升级（3）、收尾与文档（2） | 8 | 看板复用 `RequestMetric` 风格 |
| 合计 | 6 周 | | **44 人日** | |

> 若只能投入 1 人，建议只做 B1（2~3 周），把"可信"打牢再谈体验——错答率不降，做得越快用户越不信任。

---

## 9. 附录

### 9.1 现状核查命令（复核本计划的事实）

```bash
python manage.py test copilot              # 230 例
python manage.py copilot_eval              # 303 条矩阵，不调模型
python manage.py shell -c "from copilot.models import *; print(CopilotConversation.objects.count(), CopilotRun.objects.count())"
grep -n "icontains" copilot/tools/search.py     # 知识库检索现状
grep -c "feedback" static/copilot/panel.js      # 前端反馈入口（当前为 0）
grep -n "MAX_HISTORY\|HISTORY_CHAR_LIMIT" copilot/engine/planner.py

# 本地应答率 / 时延 / 工具被拒（本文基线数据的复算方式）
python manage.py shell -c "
from copilot.models import CopilotRun as R, CopilotToolCall as T
import statistics as st
ds = sorted((r.finished_at - r.created_at).total_seconds() for r in R.objects.exclude(finished_at=None))
local = sorted((r.finished_at - r.created_at).total_seconds() for r in R.objects.exclude(finished_at=None).filter(total_tokens=0))
print('本地应答率', round(100 * R.objects.filter(total_tokens=0).count() / R.objects.count()), '%')
print('时延中位', round(st.median(ds), 2), 'P95', round(ds[int(len(ds)*0.95)-1], 2))
print('本地直答中位', round(st.median(local), 3))
print('工具被拒', T.objects.filter(permitted=False).count(), '/', T.objects.count())
"
```

### 9.2 术语

| 术语 | 含义 |
| --- | --- |
| 本地链路 | 不经模型的确定性回答（识别 + 查询 + 模板拼装） |
| 金样集 | 有固定期望结果、可重复运行的模型链路用例 |
| 槽位 | 会话级记录的结构化条件（模块/关键词/状态/时间），用于多轮继承 |
| 本地应答率 | 未调用模型的问答占总问答的比例 |

### 9.3 相关文档

- `docs/AI_Copilot_开发方案_20261031.md`：§1~§28，全部设计决策与缺陷复盘
- `docs/AI_Copilot_评估报告_20261031.md`：303 条矩阵结果
- `copilot/eval_matrix.py`：评估矩阵实现
- `notifications/scheduler.py`：调度器（B3 复用）
- `records/expiry.py`：到期口径（B3 简报复用）

## 10. 落地记录（2026-10-31 执行）

按本计划书完成第一批落地，状态如下：

| 项 | 计划编号 | 状态 | 说明 |
| --- | --- | --- | --- |
| 反馈闭环（赞/踩 → 评估集 → 周报） | B1-1 | ✅ | `CopilotFeedback` 模型 + `POST /copilot/feedback/`；`copilot_promote` 把 7 天内的踩转为 `CopilotEvalCase` 草稿（`active=False`）；`copilot_feedback_report` 输出不满意率 Top10 并写 `TaskRunLog` |
| 会话管理 + 保留策略 | B1-2 | ✅ | `copilot:conversations/rename/delete`（软删 `archived`）；`copilot_prune` 按 `retention_days` 清会话与孤儿 run，外发日志单独 `egress_retention_days`（默认 180），`--dry-run` + `--confirm` 双闸 |
| 模型链路金样集 | B1-3 | ✅ | `copilot/model_cases.py`（约 55 例）+ `copilot_eval --with-model --model-out docs/eval/xxx.json`，三重判定（工具调用/数字与引用/关键词与禁词），每题 2 次取稳 |
| 历史回答按当前权限复核 | B1-4 | ✅ | `_conversation_data` 逐条按当前权限复核，无权模块引用隐去正文并标注 `is_restricted` |
| 流式输出（SSE） | B2-1 | ✅ | `copilot/streaming.py` 进程内事件总线 + `GET /copilot/run/<id>/stream/`；前端 EventSource 优先、轮询兜底；非原生工具协议仍走整段返回 |
| 多轮跟问（槽位） | B2-2 | ✅ | `copilot/engine/slots.py`：每轮把模块/关键词写进 `conversation.slots`，下一句"那这个客户呢"自动展开；步骤区会显示 `slots` 一项便于排查串话 |
| 沉淀导出 | B2-3 | ✅ | 回答操作行加「导出 Excel」（`copilot/export.py`，按引用逐模块导出）与「存知识库草稿」（`status=draft`，visibility=private，写审计） |
| 场景入口「问 AI」 | B2-4 | ✅ | 客户台账/诉讼列表行与详情页入口，点开面板并预填问题（不自动发送） |
| 窄屏适配 | B2-5 | ✅ | ≤560px 全屏抽屉、隐藏缩放把手；历史抽屉（最近 20 会话，切换/重命名/删除） |
| 每日简报 | B3-1 | ✅ | `copilot/digest.py` 复用 `records/expiry.py` 到期口径 + 系统会话 + `emit('copilot_daily_digest')`（同日去重），`copilot_daily_digest --dry-run/--username/--confirm` |
| 确定性规则提醒 | B3-2 | ✅ | `copilot/rules.py` 三条规则（客诉超期未闭环 / 保险临期 30 天 / 台账额度占用≥90%），`copilot_alerts` 干跑默认、`--confirm` 发通知 |
| 质量成本看板 | B3-3 | ✅ | `copilot/metrics.py` + `/copilot/quality/` 页面（本地应答率、时延分位、token、失败码、外发拦截、反馈不满意率），侧边栏「AI 助手」组下新增入口 |
| 检索排序与外发审计 | B3-4 | ✅ | `copilot/ranking.py` BM25（标题加权）接入知识库工具；`/copilot/egress/` 外发审计查询页 |
| 附带加固 | — | ✅ | `copilot/tools/daterange.py`（日期区间抽取，含倒序交换修复）+ `text_utils.py`（slug/重音折叠）+ `alias_map.py`（跨写法别名登记）；`MetalPrice` 增 (品名, 日期) 复合索引 |

**验证**：`manage.py test copilot` 287 例全过（本轮从 253 → 287）；`makemigrations --check` 无漂移；`collectstatic` 已执行（静态版本 `20261031q`）。

**渠道决策（2026-10-31 确认）**：每日简报**只走站内通知**，不加邮件渠道——这是定案，不是缺口。§4 B3-1 原计划"站内 + 邮件"不再执行。理由：简报的使用场景是"上班打开系统先看一眼"，站内通知 + 系统会话已经覆盖；邮件反而引入 SMTP 配置、邮箱缺失、被当垃圾邮件三类运维负担。通知体系本身的能力不受影响——模板的邮件内容仍保留在种子里，将来若某个模板（如规则提醒）确需邮件，管理员在通知中心启用即可，代码零改动。

**第二批补充（2026-10-31 晚）**：
- ✅ `copilot_prune` 接入调度器落地：迁移 0006 把已登记任务从"每晚干跑"升级为真删（`params={"confirm": true}`，只升级管理员未改过的空参数行）；手工执行仍默认 dry-run 双闸。
- ✅ 显式时间口径统计接入确定性链路：`tools.daterange` 接进 `stat_query`/`sum_query`/`rank_query`——"2026年3月的客诉有多少""2024 Q3 赔付最多"本地直算并附口径说明；"今年/上月"等相对口径仍交模型（边界刻意保留）。评估矩阵新增 `time` 用例（动态取最新记录月份），313 例全过。
- ✅ 模型链路基线已跑通并落盘（`docs/eval/模型链路基线_20261031.md`）：**61 例，通过率 52.5%，抖动率 0%，平均 6833 token / 4.6s**。失败三类：工具调错 17（跨模块关联/指代/知识问答）、查错模块 11（账期/核价/借阅等需字段级路由）、答案偏离 1。这条 52.5% 就是换模型/改路由前的对照基线，失败明细即第二批"跨模块关联推理 + 模型路由"的输入。

**仍留在下一批**：B3 看板的历史趋势曲线；规则提醒"不再提醒"与看板按周筛选两个小交互；跨模块关联推理。
