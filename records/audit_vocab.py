"""操作留痕的**词表单一事实源**（模块名 + 动作名 + 历史同义词）。

存在的原因（一致性问题审计 2026-10-25）：
同一动作在不同模块被写成三种叫法 —— ``records/crud.py`` 写「更新」、
``samples/services.py`` 写「编辑」、``knowledge_base/views.py`` 写「文档创建」；
而 ``records/views_audit.py`` 的异常检测按字面值
``operation_type__in=["删除", "编辑"]`` 过滤，于是一半模块的编辑操作
**根本不进入检测**（导出爆量同理，档案的「下载」不计入）。

本模块做三件事：
1. ``normalize_action`` / ``normalize_module``：写入时把同义词折叠成标准词，
   新数据从此一致（**不改历史数据** —— 留痕表不重写是审计底线）；
2. ``expand_*``：把标准词展开成「标准词 + 历史同义词」，供查询/规则使用，
   历史行照样命中，不需要数据迁移回填；
3. ``MODULE_CHOICES`` / ``ACTION_CHOICES``：给页面下拉用，不再硬编码，
   改词表只改这一处。

规则：**只增不改**。新增模块/动作往 ``EXTRA_ACTIONS`` / ``MODULES`` 里加，
不要在业务代码里直接写中文字面量（找不到词就记 warning，便于发现遗漏）。
"""

import logging

logger = logging.getLogger(__name__)

# ============================== 模块 ==============================

# 标准模块名（页面下拉、审计规则、写入校验都以此为唯一来源）
MODULES = (
    "每日记录",
    "装修",
    "客户账期",
    "客户客诉",
    "客户台账",
    "诉讼",
    "制度及处罚",
    "核价",
    "保险",
    "金属价格",
    "数据展示",
    "数据下载",
    "知识库",
    "档案管理",
    "证照管理",
    "试样管理",
    "到期预警",
    "系统管理",
    "通知中心",
    "AI 助手",
)
# 历史同义词 → 标准模块名（老数据里出现过，查询时一并命中）
MODULE_SYNONYMS = {
    "档案": "档案管理",
    "证照": "证照管理",
    "样品": "试样管理",
    "试样": "试样管理",
}


# ============================== 动作 ==============================

class Action:
    """标准动作词。业务代码引用这里的常量，不要写中文字面量。"""

    CREATE = "新增"
    UPDATE = "编辑"
    DELETE = "删除"
    STATUS = "状态变更"
    EXPORT = "导出"
    DOWNLOAD = "下载"
    IMPORT = "导入"
    BACKUP = "备份"
    RESTORE = "恢复"


# 五类核心动作（CRUD + 状态流转），审计规则的默认关注面
CORE_ACTIONS = (
    Action.CREATE,
    Action.UPDATE,
    Action.DELETE,
    Action.STATUS,
)

# 领域动作：语义独立、无法折叠进 CRUD，但同样要能被筛出来
EXTRA_ACTIONS = (
    # 档案 / 证照
    "年审",
    "借阅",
    "归还",
    "移动",
    "上传附件",
    "删除附件",
    # 账户与系统管理
    "重置密码",
    "修改密码",
    "撤销会话",
    "撤销Token",
    "AI配置",
    # 通知中心
    "发布公告",
    "撤回",
    "任务配置",
    "手动执行",
    "偏好修改",
    # 业务专有
    "续保",
    "批量沉淀",
    "可见性变更",
    "大纲保存",
    # AI 助手（Copilot）
    "AI 问答",
    "AI 工具调用",
    "AI 生成草稿",
    "AI 外发数据",
)

# 全部已知动作（写入校验用）
ALL_ACTIONS = (Action.CREATE, Action.UPDATE, Action.DELETE, Action.STATUS,
               Action.EXPORT, Action.DOWNLOAD, Action.IMPORT,
               Action.BACKUP, Action.RESTORE) + EXTRA_ACTIONS

# 历史同义词 → 标准动作：
# 把「对象类型」从动作名里剥出去（对象已经在 object_identifier 里），
# 把「更新/编辑」这类纯叫法差异折叠成一种。
ACTION_SYNONYMS = {
    # 编辑
    "更新": Action.UPDATE,
    "修改": Action.UPDATE,
    "文档编辑": Action.UPDATE,
    "分类编辑": Action.UPDATE,
    "文件夹重命名": Action.UPDATE,
    # 新增
    "创建": Action.CREATE,
    "文档创建": Action.CREATE,
    "分类创建": Action.CREATE,
    "文件夹创建": Action.CREATE,
    # 删除
    "文档删除": Action.DELETE,
    "分类删除": Action.DELETE,
    "文件夹删除": Action.DELETE,
    # 状态
    "文档归档": Action.STATUS,
    "归档": Action.STATUS,
    # 导出 / 下载
    "批量导出": Action.EXPORT,
    "模板下载": Action.DOWNLOAD,
    "导入模板": Action.DOWNLOAD,
}


def normalize_module(module):
    """把模块名折叠成标准词；未知模块原样返回并记 warning。"""
    module = (module or "").strip()
    canonical = MODULE_SYNONYMS.get(module, module)
    if canonical and canonical not in MODULES:
        logger.warning("留痕模块名不在词表中：%r（请在 records/audit_vocab.py 补充）", module)
    return canonical


def normalize_action(action):
    """把动作名折叠成标准词；未知动作原样返回并记 warning。"""
    action = (action or "").strip()
    canonical = ACTION_SYNONYMS.get(action, action)
    if canonical and canonical not in ALL_ACTIONS:
        logger.warning("留痕动作名不在词表中：%r（请在 records/audit_vocab.py 补充）", action)
    return canonical


# ============================== 查询展开 ==============================

def _with_synonyms(canonical):
    """返回 {标准词} ∪ {指向它的历史同义词}，用于 ``__in`` 查询。"""
    values = [canonical]
    values += [k for k, v in ACTION_SYNONYMS.items() if v == canonical and k not in values]
    return tuple(values)


def expand_action(action):
    """单个动作 → 可用于 ``operation_type__in`` 的元组（含同义词）。"""
    return _with_synonyms(normalize_action(action))


def expand_module(module):
    """单个模块 → 可用于 ``module_type__in`` 的元组（含同义词）。"""
    canonical = normalize_module(module)
    values = [canonical]
    values += [k for k, v in MODULE_SYNONYMS.items() if v == canonical and k not in values]
    return tuple(values)


def expand_actions(actions):
    """多个动作合并展开（去重，保持顺序）。"""
    out = []
    for action in actions:
        for value in expand_action(action):
            if value not in out:
                out.append(value)
    return tuple(out)


# 审计规则用的常用组合（已含历史同义词，历史数据无需回填即可命中）
DELETE_ACTIONS = expand_action(Action.DELETE)
UPDATE_ACTIONS = expand_action(Action.UPDATE)
EXPORT_ACTIONS = expand_action(Action.EXPORT)
WRITE_ACTIONS = expand_actions((Action.CREATE, Action.UPDATE, Action.DELETE, Action.STATUS))


# ============================== 下拉选项 ==============================

# 顺序即下拉顺序：核心动作在前，领域动作按业务组顺序
MODULE_CHOICES = MODULES
ACTION_CHOICES = (Action.CREATE, Action.UPDATE, Action.DELETE, Action.STATUS,
                  Action.EXPORT, Action.DOWNLOAD, Action.IMPORT,
                  Action.BACKUP, Action.RESTORE) + EXTRA_ACTIONS


# ============================== 列表页筛选用 ==============================

def filter_by_module(qs, value):
    """按模块名筛选，命中标准名与其历史同义词（供 ``make_list_view`` 的 filters 用）。"""
    if not value:
        return qs
    return qs.filter(module_type__in=expand_module(value))


def filter_by_action(qs, value):
    """按动作名筛选，命中标准名与其历史同义词。"""
    if not value:
        return qs
    return qs.filter(operation_type__in=expand_action(value))
