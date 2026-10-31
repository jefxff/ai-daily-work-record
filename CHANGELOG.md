# 变更日志

本文件记录对外可见的功能、部署形态与依赖变更。格式参考 Keep a Changelog。

## [2.3.2] - 2026-11-01

**「进度为判决」句式在列表与统计两条路径上答案一致**。无数据库迁移、无接口
变更，重启 web 生效。

### 修复

- **「诉讼中进度为判决的有哪些」不再答"没有记录"**（「有几条」一直是对的，
  4 条）：列表路径剥掉选项标签"判决"后，剩下的"进度为"（字段名+系词）被拿去
  全文模糊匹配——任何记录里都没有这三个字，AND 关系下筛成 0 条。现在
  "（中/里）+（字段名/泛称）+ 否定词? + 系词 + 选项标签 + 尾粒子"整段剥除，
  「状态为审理中」「进度是判决的」「进度为判决有几条」（尾部"有"）同类句式
  一并覆盖。
- **「不是判决的」翻成排除而不是答反**：'不是/不为' 此前不在否定词表里，
  「不是判决的有几条」被判成正说、答成判决的 4 条（真值 16 件）。否定词表
  补齐后：列表路径按"排除该值"过滤；统计路径检测到否定时带否定词整个下传，
  不再拿正向值充数。
- **无同义否定选项的否定句按排除算**：「没有判决」以前落到全文检索"没有判决"
  必然 0 条；现在翻成 `progress ≠ 判决`。

### 测试

- `test_answer_regressions.py` 新增 5 例（字段前缀剥离、两路径一致、尾粒子、
  否定排除、统计否定）；全量 1117 通过（skip 1），`copilot_dict_check`
  21/84/237 通过。

## [2.3.1] - 2026-11-01

配置真实模型（chat 用途）后的第三批修复：把两边接缝上的错答补齐——"本地该算
的没算""本地算了一半""模型答坏了没人管"。**无数据库迁移、无接口变更**，
重启 web 生效。

### 修复

- **一句问两个模块时不再只答第一个**：「客诉和诉讼分别有多少」以前只算客诉
  （8 条），诉讼被原地丢掉，用户以为诉讼是 0（实际 1 件）。现在按点名的模块
  逐个算，答案分段带模块名；只识别出一个模块时，会补一句"另一个词对应哪些
  模块"（如「客户」→ 客户台账 / 客户账期 / 客户客诉），不再装作问全了。
- **「你能做什么」不再答"没有找到符合条件的记录"**：能力类问句以前只认
  "能查什么"，"能做什么/能干什么/有什么功能"漏网，交给模型后它连调两轮
  `list_modules`，最后以"没找到记录"收场。现在本地直接给出示例问法与模块清单；
  点了模块名的长句（"供应商能做哪些品类"）不会被误判成问能力。
- **模型原地打转时不再把半句废话端给用户**：7B 拿到 0 行会一路写下去
  （实测 40+ 句"无其他记录。无其他信息。"）。现在先判退化：查到 0 条就直说
  「客户台账」没有记录，有数据就列引用卡片，绝不截一半当答案。
- **回答里的裸花括号被剥掉**：模型爱把整段回答包进 `{ "…" }`，面板上就带着
  花括号显示。提示词补了"不要包裹"的约束，落地前再做一次剥壳（只在不是 JSON
  时动手）。
- **追问不再认错对象**：槽位以前把"第一条引用的标题"当关键词，问完
  「有哪些未完成的客诉」（第一条是长通精密五金厂），下一句「那上个月的呢」
  就被改写成"长通精密五金厂 上个月"。现在只记用户自己说出来的值；另外，
  很短且没点模块的追问（「赔了多少钱」）会接上上一轮的模块，不再让模型重猜。
- **「各客户的客诉数量分布」按客户分组**：以前答成"按客诉状态分布"——数字
  全真、维度全错。字段名带"名称/编号"后缀时按主体匹配（客户 → `customer_name`），
  分组维度就是记录名时不再多一列重复的"举例"。
- **「赔了多少钱」交给本地求和**：口语里很少说"赔偿金额"，只认全称时选不出
  字段，落到模型手里就成了编数字（实测答 40000，真值 80000）。现在 `赔`/`赔付`/
  `索赔` 都认，列名仍显示「赔偿金额」而不是单字。
- **模型抄了一条空记录时按"没查到"回答**：`{"客户名称":"","责任业务":""}`
  以前会渲染成一行行"字段名："的空列表。

## [2.3.0] - 2026-10-31

助手问答的第二批修复：四类"条件在半路丢了"的错答。共同点是**答案看着像答案，
其实答的不是这个问题**——比答"不会"更有害。**无数据库迁移、无接口变更**，
重启 web 生效。

### 修复

- **「最近过期的证照有哪些」不再答"没有记录"**（库里实有 5 条）：状态词剥离
  后剩下的"最近"被当成关键词去 `icontains`，没有哪个字段存着"最近过期"。
  现在残余先剥状态短语、再剥时间修饰语，剥空就置空 keyword；剩下实词也先拿去
  库里试一次（`exists`），筛得出东西才留。
- **「审厂没有通过的供应商」不再返回全是"通过"的记录**：否定判定库内收敛成
  一份（`catalog.phrase_negated`），并认多字否定词（没有/并非/从未/未曾/尚未）。
  工具层原来只看前一个字，"通过"前面是"有"，于是被判成正说。
- **「华建建材未完成的客诉」不再答成「每日记录 共 11 条」**："未完成"不是客诉
  的状态短语（它是 `complaint_status` 的选项值），状态纠偏只看短语，就把整句
  话改判到每日记录。现在**选项值命中即算"这个模块接得住"**，不再改判。
- **选项词与客户名改为"与"关系**：`apply_filters` 以前把选项条件与全文
  `icontains` 并进同一个 OR，等于把"华建建材"这个条件扔掉，答出全部未完成的
  客诉还说这就是那一家。现在先按选项精确筛，再用剥掉选项词与模块名后的实词做
  模糊筛。
- **「上个月客诉有多少」「上个月赔偿金额合计」能算了**（真值 12 条 / 69,000 元）：
  相对口径（上个月/本月/今年/上周）以前只有计数排名那一条路认识，统计/求和/
  排序三条路解析不出区间就整句推给模型，模型没配就降级成"没有记录"。现在
  `_time_gate` 统一按"显式区间 → 相对区间"两档解析，四条路径共用。
  没有刻度的"最近/近期"仍交模型，不猜。
- **「证照管理中，有哪些需要年审」不再答成"证照共 20 份"**：证照新增
  `review_due`（待年审）口径 = 下次年审日已到点（含已过期未办），实测 5 份。
  「要年审了」仍是原来那档"30 天内"（`pending_review`），两者不互相吞。
- **条件词恰好是字段选项时不再自相矛盾**：守卫以前一律说"没有对应的筛选口径"，
  下一句却列着"能筛的状态：未完成…"。现在识别为选项值，直接给"可以按选项筛"
  和示例问法。
- **模型照抄提示词占位符不再变成脏步骤**：输出格式示例原写作
  `{"tool":"工具名"}`，小模型直接照抄，`steps` 落库出现 `{'tool': '工具名'}`，
  面板上多一条点不开的记录。示例改成真实的 `query_records` 调用，占位符名
  也不再记入步骤。

### 变更

- 模块别名表从 `copilot/engine/planner.py` 移到 `copilot/catalog.py`：工具层
  剥"残余关键词"时也要知道"审厂""客诉"是模块名而不是筛选条件。`planner` 仍
  导出同名常量，外部引用不受影响。
- `sum_query` / `stat_query` / `rank_query` / `top_count_query` 的时间口径
  统一走 `_time_gate`，删除 `top_count_query` 里重复的一份相对口径代码。

### 测试

- 新增 `copilot/tests/test_answer_regressions.py`（11 例）：多字否定、
  证照残余关键词、选项值不改判、选项+客户"与"关系、年审口径两档、守卫文案、
  占位符步骤。
- 原来断言"相对口径必须交模型"的 5 处测试改为断言"本地算且数字正确"，另补
  "最近/近期仍交模型"的边界测试。
- 全量 `manage.py test --parallel 1`：**1089 通过**（skip 1）；`copilot_dict_check`
  通过（21 模块 / 84 状态 / 237 字段）。

## [2.2.0] - 2026-10-31

前端修复批次：侧栏"消息通知"重新可见、数据展示页去掉遗留搜索框、
11 个列表页的"列设置"按钮落到各自操作行的正确位置。
**无数据库迁移、无接口变更**；改了 CSS/JS 各一处版本号（`app.css?v=20261031a`、
`table-tools.js?v=20261031a`），强刷或正常过期后生效。

### 修复

- **侧栏"系统"组的"消息通知"重新可见**：模板与权限一直没动，是
  `.sidebar-dropdown.show` 的 `max-height: 32rem` 被内容高度超过了——超管的
  "系统"组现有 15 个链接（约 36.8rem），排最后的"消息通知"被整体裁掉，
  看起来像链接消失。上限提到 48rem 并允许溢出滚动。
- **数据展示页顶部的"搜索 + 搜索按钮"移除**：顶栏操作区下沉到内容区后，
  dashboard.html 里遗留的旧 `top_nav_actions` 搜索表单孤零零挂在页首，
  而视图从不消费 `search` 参数——纯死代码，删除。
- **"列设置"按钮不再悬在页首**：`table-tools.js` 早先把按钮 append 到
  `document.querySelector('.page-actions')`，命中的总是 base.html 顶部的
  **空**操作条，而档案柜/借阅记录/证照管理等页在内容里另有自己的
  `.page-actions`。改为：优先按模板声明的锚点落位
  （`data-col-settings-anchor` + `data-col-settings-position`），无锚点时
  兜底挂到最后一个非空操作条。12 个页面已声明锚点：样品台账（已终止后）、
  供应商档案（重置后）、档案柜/类别管理/证照借阅记录（导出 Excel 前）、
  借阅记录（全部前）、证照管理（重置前）、用户管理/登录日志（重置后）、
  角色权限（新建角色前）、任务历史（应用后）、长期 Token（"共 N 条"后）。

## [2.1.9] - 2026-10-31

修掉三类"明明能算却答不会"的问题：计数排名、状态词指错模块、否定词读反。
**无数据库迁移、无接口变更**，重启 web 与 `run_scheduler` 生效。

### 修复

- **「哪家客户客诉最多」不再答"没有数值字段可比较"**：这类问题的度量是
  **条数**，模块里根本没有"客诉数量"字段，数值排名接不住就落到守卫上。
  新增计数排名分支：按维度分组计数取最多，榜首给条数、参与基数与第二名
  差距，"前 N 家"给排行榜；点到真正的数值字段（"赔偿金额最多"）仍走
  原数值排名。
- **「上个月」能算了**：相对口径（上个月/本月/今年/上周）日历上无歧义，
  新增 `extract_relative_range` 换算成区间；换算不出仍交回模型，不把
  全量排行冒充成"上个月"。
- **「试样管理中已终止的有哪些」不再答"试样试产没有这个口径"**："已终止"
  是样品送样的状态，只是"试样"这个词先命中了试样试产。状态词全库恰好
  一个模块认识、且够具体时，自动把主语纠过去；纠偏回答带模块名，说错了
  一眼能看出来。两字泛词（"通过"）不参与纠偏，避免「装修中通过的」被
  劫持到试样试产。
- **「没通过」不再读成"通过"**：状态短语匹配以前是纯子串匹配，反话会被
  读成正话（「试样试产中没通过的有哪些」答成通过的有几条、"不同意"命中
  "同意"）。现在短语前紧贴否定词（没/不/无/非/未…）时不命中，除非短语
  本身以否定词开头（"不通过""未通过"）。
- **状态词不再同时当关键词双重过滤**："已终止"既是状态词又恰好是选项标签
  时，会同时变成 keyword（icontains 搜正文）+ state 两层过滤，把口径放大。

### 新增

- 「处理中」的说法补了"进行中"（客户客诉）。
- `unsupported_condition` 的"筛不了"提示会顺带指路：条件词恰好是别的模块
  的状态时，告诉用户"如果你其实想问那边，直接说「XX 中已终止的有哪些」"。


## [2.1.8] - 2026-10-31

修 Windows 启动脚本选错解释器导致**根本起不来**的问题，并顺手修掉两处让
`stop`/`status` 说谎的进程判定缺陷。**无数据库迁移、无接口变更**，
不涉及 Python 代码，重跑一次启动脚本即可。

### 修复

- **`project.bat` 报 `ModuleNotFoundError: No module named 'django'`**：
  脚本原先只在「项目内 `.venv`/`venv` → `C:\Python311` → PATH」里找 python，
  虚拟环境建在 `%USERPROFILE%\.venvs\` 时必然落到 PATH 里那个没装 Django 的解释器上。
  新增 `scripts/windows/detect_python.ps1` 作为唯一实现：读 `requirements.txt` 的
  `django==x.y.z`，逐个探测候选，**只返回装了正确版本 Django 的那个**；
  一个都没有就打排查清单与修复命令后中止，不再静默挑一个不能用的。
  `project.ps1`、`start_windows.bat`、`start_windows_https.bat` 全部改走它。
- **`stop` 打印"停止完成"但进程还在**：残留清理只认绝对路径的 `manage.py`，
  而手工运行与 `start_windows_https.bat` 用的是 `cd` 后的相对路径 `python manage.py …`，
  于是杀不掉，再启一次就多一个实例。改为绝对/相对两种形态都认。
- **`status` 把在跑的 web 报成"未运行"**：本机 `Get-NetTCPConnection -LocalPort`
  会把正在监听的端口报成 `OwningProcess=0`，过滤后当成空闲。现在过滤 PID 0 的幽灵
  记录后，再用真实 TCP 连接探测兜底——能连上就算被占用，只是身份不可读。
- **端口占用时无法自愈**：端口上若已是本项目实例（例如之前用 `-Https` 起的），
  现在按"已在运行"处理，不再直接抛"端口被占用"。

### 新增

- **`project.bat` 支持 `-Https`**：与 `start_windows_https.bat` 同一套 HTTPS 实现
  （`scripts/serve_https.py`），`restart -Https` 默认端口 8443，交互菜单也加了
  对应两项；`status`/`stop` 按模式区分端口，不再分不清 8080 与 8443。
- **`WR_PYTHON` 环境变量统一生效**：显式指定解释器，跳过自动探测。

### 其它

- 状态文件 `logs/.project-state.json` 增加 `mode` 字段，端口默认值按上次模式取，
  避免 `start -Https` 之后直接 `status` 跑去查 8080。
- 同一实例常表现为「启动器 + 真实解释器」两级同名进程（venv 的 `python.exe` 会再
  拉起一个子进程），`status` 与残留清理按实例去重，不再把一个算成两个。


## [2.1.7] - 2026-10-31

补齐 AI 助手的**字段字典与业务状态口径**，修掉"条件被静默丢掉、只报全表合计数"
这一类错误答案。**无数据库迁移、无接口变更、无需重新 collectstatic**，
重启 web 与 `run_scheduler` 即可生效。

### 修复

- **"快到期"不再被丢掉**：以前问「保险中快到期有几条」，答的是保险全表 20 份
  ——`快到期` 既不是日期表达、也不是任何模块的已声明状态，于是被整句丢弃，
  只剩一次无筛选计数。现在按模块的 `lead_days` 换算成"日期字段落在未来 N 天"
  的窗口（保险 30 天、合同/证照 60 天、日报 3 天），与工作台到期预警同源。
- **"不装懂"保护**：条件词认得、但模块没有对应口径时，不再退化成全表计数，
  改为明确说明"筛不了什么、能筛什么、日期字段是哪个"。
- **极值问题无字段**：「账期最长的是哪几家客户」以前答"共 20 家"（"最长"被忽略），
  现在明确回答该模块没有可比较的数值字段，并列出真正可排序的字段。
- **窗口天数 30/60 不一致**：字典里写死 30 天，工作台合同/证照是 60 天，同一个
  "证照快到期了吗"两个入口两个答案。窗口改为直接引用 `records/expiry.py`。
- **工具拒绝理由不可执行**：模型传了模块不支持的 `state`，以前只回"该模块不支持"，
  模型原样重试两次后答"没有记录"；现在回"可用状态是…／改用 date_from/date_to，
  日期字段是…"。

### 新增

- **业务状态铺满 21 个模块**（3 → 83 条）：保险（按标的取最新年度判到期）、诉讼、
  客户客诉（含超 SLA、涉及法律程序）、客户账期（含考核期）、客户台账（归档/担保/
  额度变更）、样品送样、试样试产、小批量、审厂、每日记录、证照年审、档案、证照、
  知识库、供应商档案。
- **`StateSpec.apply`**：支持 QuerySet 级状态（保险"一个标的下多条流水，取最新
  年度"这种判定，纯 `Q` 表达不了）。
- **`ModuleSpec.lead_days` / `no_state_reason`**：到期窗口参数化；确实没有状态的
  模块显式写明原因，模型不再硬造 `state` 参数。
- **补齐 43 条业务字段声明**（237 条总数）：合同问题/担保信息、考核期四件套、
  报保险与 SLA、样品登记与终止、试样通知、归档编号与日期等。
- **`manage.py copilot_dict_check`**：字典自检。字段声明与模型对不上直接报错；
  未进字典的业务字段报警告（`--strict` 视为错误）；`--export` 导出 Markdown
  供业务方校对中文名（已生成 `docs/字段字典_20261031.md`）。
- **测试**：新增 `copilot/tests/test_dictionary.py`（25 例），含"保险到期数必须
  等于工作台预警条数"这条口径断言。

### 其它

- 修复 `copilot/tools/daterange.py` 用进程本地日期（违反项目时区规约）。
- 月历两个测试的夹具日期改为固定落在本月，不再随月末前后漂移而红。
- `notifications` 种子任务测试补齐 Copilot 的四个内置任务。

## [2.1.6] - 2026-10-29

新增「日志中心」（左侧「系统」→「日志中心」，`/system/logs/`）。**既有页面、URL
与业务数据零改动**；仅新增一张日志表和只读页面，权限复用 `system.read`。

### 新增

- **日志入库**：`SystemLog` 表 + `notifications/logs_service.py` 增量采集 `logs/`
  白名单文件；按字节偏移续读，轮转/截断自动归零，`fingerprint` 唯一约束去重；
  多行异常堆栈归并为一条；`password/token/SECRET_KEY` 等敏感值入库前掩码。
- **日志中心页面**：近 24 小时摘要卡、级别分布、错误来源 Top5、纳管文件采集进度、
  分页日志流（行内展开完整堆栈）；级别 / 文件 / 关键字 / 时间窗筛选；≤640px 卡片模式。
- **手动采集**：`POST /system/logs/ingest/`（system.update），返回新增/重复/裁剪条数。
- **CSV 导出**：`GET /system/logs/export/`（system.read），走 `@guarded_export`、
  行数上限与公式注入防护，并写入操作留痕。
- **API**：`GET /api/system/logs/`（system.read，level/source/search/days/page/per_page）。
- **配置**：`LOG_INGEST_ENABLED`、`LOG_INGEST_FILES`、`LOG_INGEST_MAX_BYTES`、
  `LOG_INGEST_MAX_MESSAGE`、`LOG_INGEST_SKIP_LOGGERS`、`LOG_MASK_SENSITIVE`、
  `LOG_RETENTION_DAYS`、`LOG_MAX_ROWS`、`LOG_INGEST_THROTTLE_SECONDS`。

### 部署

1. `python manage.py migrate`（新增 `notifications.0015_systemlog`）
2. `python manage.py collectstatic --noinput`（新增 `static/js/pages/log_center.js`）
3. 重启 web 与 `run_scheduler` 进程

## [2.1.5] - 2026-10-26

补齐 A 类业务页面缺失的 DRF API。**无 URL、模板、数据与部署步骤变化**；
页面行为不变，新接口与页面共用同一套数据口径。

### 新增（A 类业务 API）

- **保险标的 / 保单年度只读**：`GET /api/insurance/subjects/`、
  `GET /api/insurance/policy-years/`（按 `kind` / `subject` / `year` 过滤）。
- **个人报告 JSON**：`GET /api/daily-record/weekly-report/`、
  `GET /api/daily-record/annual-summary/`（仅本人记录，与页面同一函数取数）。
- **到期日历聚合**：`GET /api/expiry/calendar/`（`days` / `day` 参数，跨
  insurance / archive 任一 read 即可，条目按用户权限过滤）。
- **操作留痕只读 + 异常检测**：`GET /api/operation-logs/`（模块/动作/操作人/
  日期筛选）、`GET /api/operation-logs/anomalies/`（四类异常，复用
  `records.views_audit.collect_anomalies`）。
- **数据下载汇总**：`GET /api/data-download/summary/`（按模块 read 权限裁剪，
  每日记录仅统计本人）。
- **知识库文件夹 CRUD**：`/api/knowledge/folders/`（list/retrieve/create/
  patch/delete + `move`），一律走 `knowledge_base.folders` 服务层并按 owner
  隔离；删除 = 内容上提，写操作全部留痕。
- **档案表级导入导出**：`/api/archive/tables/<table>/export/`、`template/`、
  `import/`（默认 `dry_run=true` 只预览，`false` 时按页面同一套匹配键与逐行
  事务 upsert）。导出接 `@guarded_export`，超 `EXPORT_MAX_ROWS` 返回 413。

### 重构（页面与 API 共用取数）

- `records/views_reports.py` 抽出 `_weekly_payload()` / `_annual_payload()`；
  `records/views_audit.py` 抽出 `collect_anomalies()`。页面视图改为调用同一函数，
  输出与改造前一致。

### 修复（测试稳定性）

- `knowledge_base/tests_sync_view.py`：`DailyRecord.record_date` 是
  `auto_now_add`，创建时传参被忽略，原断言写死 `2026-10-25` 只在当天通过；
  改为创建后 `update()` 固定日期，用例恢复确定性。

### 测试

- 新增 `ai_api/tests_business_api.py`（14 个用例）；全量 **639 个测试通过**
  （1 跳过），`check` 无问题，`makemigrations --check` 无变化。

## [2.1.4] - 2026-10-25

跨模块技术实现一致性整改（审计报告 `impl-consistency-audit-2026-10-25.md`）。
核心变化：把「同一件事的多套写法」收敛成单一出处，并修掉因此产生的功能缺陷。

### 新增（约定的单一出处）

- **`records/audit_vocab.py`**：模块名 / 动作名 / 历史同义词的唯一词表。
  写入时归一（`normalize_*`），查询时展开（`expand_*`），页面下拉直接取
  `MODULE_CHOICES` / `ACTION_CHOICES`。
- **`work_record/file_validation.py`**：文件头（magic byte）判定的唯一实现。
  各模块只保留自己的白名单与大小上限。
- **`work_record/export_guard.py::guarded_export`**：导出并发闸门的统一装饰器。
- **`docs/api-conventions.md`**：留痕、响应外壳、状态码、权限写法、分页、上传、
  导出、时间、日志、事务十条约定，作为横切规则的唯一出处。

### 修复（P0，有功能影响）

- **留痕动作词三套 → 归一**：records 记「更新」、samples 记「编辑」、知识库记
  「文档创建」，而审计规则按字面值 `operation_type__in=["删除","编辑"]` 过滤，
  导致 records 与知识库的编辑操作**完全不进入**「非工作时间修改」检测；
  「导出爆发」也只认「导出」，档案的「下载」不计入。现在写入归一、规则用
  `expand_*` 展开同义词，历史行不动也能命中。
- **操作人口径混存 → 新增 `OperationLog.operator_username`**（迁移 `0018`）：
  `operator` 保留展示名（姓名/用户名），账号单独落库并在迁移中回填历史行
  （`系统管理员` → `admin` 这类唯一映射自动还原，重名不猜）。四处异常检测
  改为按 `actor`（账号优先、展示名兜底）分组，同一个人不再被拆成两个。
- **留痕页下拉硬编码 → 由词表生成**：补齐「通知中心」模块与
  ／重置密码／撤销会话／发布公告／任务配置／手动执行／撤回 等动作
  （上一轮新增的账户与通知留痕此前在页面上筛不出来）。筛选同样支持旧词。
- **导出闸门只覆盖 3/6 → 全覆盖**：`ledger_export`、`ledger_batch_export`、
  `export_archives`、`expiry_export` 接入 `@guarded_export`；台账批量导出与
  表级导出补行数上限（超限 413）。新增覆盖守卫测试，未来新增导出漏挂会直接红。
- **archive 模块 42 处冗余 `@login_required` 移除**（共 46 处，另 4 处随导出改造
  一并去掉）：它叠在 `@perm_required` 外层，导致匿名 AJAX 拿到 302 HTML 而不是
  全站统一的 401 JSON，前端 `response.json()` 直接抛异常。

### 修复（P1，形式统一）

- **三份 magic byte 表合并为一份**：`samples/validators.py`、
  `archive/validators.py`、`archive/forms.py` 各有一份且宽严不一
  （PNG 4 字节 vs 8 字节、PDF `%PDF-` vs `%PDF`）。现统一，同名格式取最严前缀。
- **响应外壳统一**：`samples` 的 2 处 `{"error": ...}` 与 `views_ai` 的 2 处
  `{"success": ...}` 改为 `{"ok": ...}`；前端同步改为读 `message`。
- **分页统一**：通知任务历史、操作留痕列表改用 `_per_page`（10/30/50）。
- **日志**：`knowledge_base/views.py` 13 处函数体内 `import logging` 改为模块级 logger。
- **去重**：删除 `records/crud.py` 与 `samples/exporters.py` 里逐字相同的
  `_sanitize_xlsx_formulas` 转发封装，直接引用 `work_record.export_utils`。

### 行为变化（需知悉）

- 新写入的留痕动作名会归一：`更新`→`编辑`、`文档创建/分类创建`→`新增`、
  `文档删除/分类删除`→`删除`、`文档归档`→`状态变更`。**历史行不改写**
  （留痕表不重写是审计底线），靠查询展开兼容。
- 迁移 `records.0018` 会加列并回填 `operator_username`，反向操作是清空该列。
- 档案模块的 AJAX 未登录响应从 302 HTML 变成 401 JSON（与其他模块一致）。
- 供应商类型接口的业务错误从 400 改为 422。

### 测试

- 全量 **615 个测试通过**（新增 12 个：词表归一/展开/筛选、操作人账号落库、
  导出闸门覆盖与 429、上传校验跨模块一致性）；`check` 无问题；
  `makemigrations --check` 无变化；ruff 全绿。

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
