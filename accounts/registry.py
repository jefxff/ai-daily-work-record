"""RBAC 模块注册表 —— 全站模块与权限码的唯一事实源。

新增业务模块时：
1. 在 MODULES 登记 key / 中文名 / 支持的动作集合；
2. 执行 `python manage.py sync_rbac` 补齐权限码；
3. 在视图加 @perm_required、侧边栏加 {% can_view %}。
"""

# 四个标准动作
CREATE = "create"
READ = "read"
UPDATE = "update"
DELETE = "delete"

ACTION_LABELS = {
    CREATE: "新增",
    READ: "读取",
    UPDATE: "编辑",
    DELETE: "删除",
}

CRUD = [CREATE, READ, UPDATE, DELETE]
READ_UPDATE = [READ, UPDATE]
READ_ONLY = [READ]

# key 同时用于权限码前缀（如 samples.create）、装饰器参数、模板标签参数
MODULES = [
    {"key": "daily_record", "name": "每日记录", "actions": CRUD},
    {"key": "decoration", "name": "装修", "actions": CRUD},
    {"key": "customer_credit", "name": "客户账期", "actions": CRUD},
    {"key": "customer_complaint", "name": "客户客诉", "actions": CRUD},
    {"key": "complaint", "name": "诉讼", "actions": CRUD},
    {"key": "regulation", "name": "制度及处罚", "actions": CRUD},
    {"key": "price_evaluation", "name": "核价", "actions": CRUD},
    {"key": "insurance", "name": "保险", "actions": CRUD},
    {"key": "metal_price", "name": "金属价格", "actions": READ_UPDATE},
    {"key": "data_view", "name": "数据展示/下载", "actions": READ_ONLY},
    {"key": "knowledge", "name": "知识库", "actions": CRUD},
    {"key": "samples", "name": "试样管理", "actions": CRUD},
    # 档案管理：验收前仅超管可见，故排除在 staff/viewer 授权范围外；
    # 验收通过后将其从 SUPERUSER_ONLY_MODULES 移除即可对普通角色开放。
    {"key": "archive", "name": "档案管理", "actions": CRUD},
    {"key": "system", "name": "系统管理", "actions": CRUD},
]

# 验收前仅超管可见的模块（不授予 staff/viewer，也暂不执行 sync_rbac 写入 admin 角色）
SUPERUSER_ONLY_MODULES = ["archive"]

# 业务模块（内置角色 staff/viewer 授权范围，不含 system 及超管独占模块）
BUSINESS_MODULE_KEYS = [
    m["key"] for m in MODULES if m["key"] != "system" and m["key"] not in SUPERUSER_ONLY_MODULES
]
MODULE_KEYS = [m["key"] for m in MODULES]
MODULE_MAP = {m["key"]: m for m in MODULES}


def permission_code(module_key, action):
    """生成全局唯一权限码，如 samples.create。"""
    return f"{module_key}.{action}"


def all_permission_specs():
    """返回 [(code, module, action, 中文名), ...]。"""
    specs = []
    for module in MODULES:
        for action in module["actions"]:
            specs.append(
                (
                    permission_code(module["key"], action),
                    module["key"],
                    action,
                    f"{module['name']} · {ACTION_LABELS[action]}",
                )
            )
    return specs


def all_permission_codes():
    return [s[0] for s in all_permission_specs()]


def module_choices():
    return [(m["key"], m["name"]) for m in MODULES]


def actions_of(module_key):
    module = MODULE_MAP.get(module_key)
    return list(module["actions"]) if module else []


def is_valid_code(code):
    return code in set(all_permission_codes())


# 内置角色定义：code -> (名称, 说明, 授权模块集合, 授权动作集合)
BUILTIN_ROLES = {
    "admin": (
        "管理员",
        "全部模块的增删改查及系统管理权限",
        MODULE_KEYS,
        CRUD,
    ),
    "staff": (
        "普通员工",
        "全部业务模块的读取、新增与编辑权限（无删除、无系统管理）",
        BUSINESS_MODULE_KEYS,
        [CREATE, READ, UPDATE],
    ),
    "viewer": (
        "只读用户",
        "全部业务模块的只读权限",
        BUSINESS_MODULE_KEYS,
        [READ],
    ),
}


def builtin_role_codes(role_code):
    """内置角色应拥有的权限码列表（并校验模块确实支持该动作）。"""
    _name, _desc, module_keys, actions = BUILTIN_ROLES[role_code]
    codes = []
    for key in module_keys:
        supported = set(actions_of(key))
        for action in actions:
            if action in supported:
                codes.append(permission_code(key, action))
    return codes
