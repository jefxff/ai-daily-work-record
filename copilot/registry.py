"""工具注册表：权限、参数校验、执行与留痕的唯一入口。

为什么权限放在这里而不是提示词里：模型可以被诱导，代码不会。任何工具
执行前都实时调用 ``has_perm``，并把"被拒绝"也写进 CopilotToolCall——
事后能查出模型是否在试探越权，而不是只在日志里留一行文本。
"""

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date

from accounts.services import has_perm

from . import catalog, security
from .models import CopilotToolCall

logger = logging.getLogger(__name__)

TOOLS = {}


@dataclass
class Param:
    name: str
    type: str = 'string'          # string | int | date | bool | module | enum
    required: bool = False
    enum: tuple = ()
    minimum: int = None
    maximum: int = None
    default: object = None
    description: str = ''


@dataclass
class ToolSpec:
    name: str
    description: str
    handler: object
    params: tuple = ()
    module_param: bool = False    # module 由参数决定
    module_key: str = ''          # 固定模块（元工具为空）
    action: str = 'read'
    level: str = 'read'           # read | draft | write
    examples: tuple = ()


@dataclass
class ToolContext:
    user: object
    conversation: object
    run: object = None
    is_local: bool = True
    policy: object = None


@dataclass
class ToolResult:
    ok: bool = True
    data: object = None
    error: str = ''
    message: str = ''
    row_count: int = 0
    truncated: bool = False
    refs: list = field(default_factory=list)

    def as_payload(self):
        payload = {'ok': self.ok}
        if self.ok:
            payload['data'] = self.data
        else:
            payload['error'] = self.error
            payload['message'] = self.message
        if self.truncated:
            payload['truncated'] = True
        return payload


def tool(name, description, params=(), module_param=False, module_key='',
         action='read', level='read', examples=()):
    def deco(fn):
        TOOLS[name] = ToolSpec(
            name=name, description=description, handler=fn,
            params=tuple(params), module_param=module_param,
            module_key=module_key, action=action, level=level,
            examples=tuple(examples),
        )
        return fn
    return deco


def get(name):
    return TOOLS.get(name)


# 工具名里出现这些词，说明模型想干的是写操作（系统只开放查询）
WRITE_INTENT_RE = re.compile(
    r'(delete|update|create|insert|remove|drop|write|import|upload|export'
    r'|删除|修改|新增|更新|导入|上传|导出|编辑)',
    re.I,
)


def resolve_tool_name(name):
    """把模型给的"工具名"规范成真实工具，返回 ``(真实工具名, 需注入的模块 key)``。

    本地小模型很容易把**模块 key 当工具名**：提示词里「可访问模块」列的是
    ``metal_price（金属价格）``，紧接着「可用工具」列 ``query_records``，
    7B 经常直接输出 ``{"tool":"metal_price"}``。这不是越权、也不是写操作，
    意图就是"查这个模块"，应当改写成 ``query_records(module=metal_price)``，
    而不是回一句"不能执行该操作"——那会让用户以为助手什么都不会。

    第二个返回值为空串表示不需要注入 module 参数。
    """
    if not name:
        return name, ''
    if name in TOOLS:
        return name, ''
    if name in catalog.MODULE_MAP:
        return 'query_records', name
    return name, ''


def is_write_intent(name):
    """工具名看起来像写操作（删除/修改/导入…），用于区分话术。"""
    return bool(name) and bool(WRITE_INTENT_RE.search(name))


def known_names_hint(limit=10):
    """给模型的"你到底能调什么"提示。

    模块 key 全列：只列前 10 个时，档案/证照/知识库永远在后面被截掉，
    模型明明能查也不知道有这个 key。
    """
    tools = ', '.join(sorted(TOOLS)[:limit])
    modules = ', '.join(spec.key for spec in catalog.MODULES)
    return f'可用工具：{tools}；可用模块 key：{modules}'


# ------------------------------------------------------------------ 可选集合

def available_modules(user, is_local=True, pol=None):
    """当前用户在当前模型下落可查询的模块（权限 + 外发策略双重过滤）。

    云端模型下，未放行的 L3 模块**不出现在清单里**——模型看不到这个选项，
    比"看到后被拒绝"少一轮无效调用，也减少信息泄露。
    """
    pol = pol or security.policy()
    result = []
    for spec in catalog.MODULES:
        if not has_perm(user, catalog.perm_of(spec), 'read'):
            continue
        if not security.spec_allowed_for_egress(spec, is_local, pol):
            continue
        result.append(spec)
    return result


def available_specs(user, is_local=True, pol=None):
    modules = available_modules(user, is_local, pol)
    keys = {m.key for m in modules}
    specs = []
    for spec in TOOLS.values():
        if spec.module_param:
            if keys:
                specs.append(spec)
        elif spec.module_key:
            if spec.module_key in keys:
                specs.append(spec)
        else:
            specs.append(spec)
    return specs


# ------------------------------------------------------------------ 参数校验

def _coerce(spec_param, value, user, is_local, pol):
    """单参数类型转换与校验，返回 (value, error)。"""
    ptype = spec_param.type
    if ptype == 'module':
        # 只做形状校验：是否存在、是否有权限、能否外发，全部交给 run_tool
        # 统一判定，这样错误码能区分"未知模块/无权限/禁止外发"。
        return str(value)[:32], ''
    if ptype == 'enum':
        if value not in spec_param.enum:
            return None, f'{spec_param.name} 取值必须是 {list(spec_param.enum)} 之一'
        return value, ''
    if ptype == 'int':
        try:
            ivalue = int(value)
        except (TypeError, ValueError):
            return None, f'{spec_param.name} 必须是整数'
        if spec_param.minimum is not None and ivalue < spec_param.minimum:
            return None, f'{spec_param.name} 不能小于 {spec_param.minimum}'
        if spec_param.maximum is not None and ivalue > spec_param.maximum:
            ivalue = spec_param.maximum
        return ivalue, ''
    if ptype == 'date':
        try:
            date.fromisoformat(str(value))
        except ValueError:
            return None, f'{spec_param.name} 必须是 YYYY-MM-DD'
        return str(value), ''
    if ptype == 'bool':
        if isinstance(value, bool):
            return value, ''
        return str(value).lower() in {'1', 'true', 'yes'}, ''
    # string
    text = str(value)
    limit = spec_param.maximum or 200
    return text[:limit], ''


def validate(spec, raw_args, user, is_local, pol):
    """校验并清洗参数：忽略未知参数（模型常多给），返回 (cleaned, errors)。"""
    if not isinstance(raw_args, dict):
        return {}, ['参数必须是 JSON 对象']
    cleaned, errors = {}, []
    declared = {p.name: p for p in spec.params}
    for name, param in declared.items():
        if name not in raw_args or raw_args[name] in (None, ''):
            if param.required:
                errors.append(f'缺少参数 {name}')
            elif param.default is not None:
                cleaned[name] = param.default
            continue
        value, error = _coerce(param, raw_args[name], user, is_local, pol)
        if error:
            errors.append(error)
        else:
            cleaned[name] = value
    return cleaned, errors


def resolve_module_key(spec, args):
    if spec.module_param:
        return args.get('module', '')
    return spec.module_key


# ------------------------------------------------------------------ 执行

def _digest(data):
    try:
        raw = json.dumps(data, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        raw = str(data)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]


def _args_hint(spec, ctx):
    """把「可用参数与取值」附在错误信息后面。

    小模型改不对参数，多数情况是它不知道有哪些可选项；把枚举/模块 key 列出来，
    下一轮它才有机会自我纠正（否则只会换个字段名继续试错）。
    """
    bits = []
    pol = ctx.policy or security.policy()
    for param in spec.params:
        if param.type == 'module':
            allowed = [m.key for m in available_modules(ctx.user, ctx.is_local, pol)]
            bits.append(f"{param.name}(可选: {', '.join(allowed[:12])})")
        elif param.type == 'enum':
            bits.append(f"{param.name}(可选: {', '.join(str(e) for e in param.enum)})")
        elif param.type == 'int':
            low = param.minimum if param.minimum is not None else 1
            bits.append(f'{param.name}(整数 {low}~{param.maximum})' if param.maximum
                        else f'{param.name}(整数 ≥{low})')
        elif param.type == 'date':
            bits.append(f'{param.name}(YYYY-MM-DD)')
    return ('；可用参数：' + '，'.join(bits)) if bits else ''


def run_tool(spec, ctx, raw_args):
    """执行一个工具，全程留痕。任何异常都收敛成 ToolResult，不抛给上层。"""
    started = time.time()
    pol = ctx.policy or security.policy()
    args, errors = validate(spec, raw_args, ctx.user, ctx.is_local, pol)
    if errors:
        message = '; '.join(errors) + _args_hint(spec, ctx)
        return _record(ctx, spec, raw_args, False, '参数错误: ' + '; '.join(errors)[:100],
                       ToolResult(ok=False, error='BAD_ARGS', message=message),
                       started, row_count=0)

    module_key = resolve_module_key(spec, args)
    if module_key:
        if module_key not in catalog.MODULE_MAP:
            return _record(ctx, spec, args, False, '未知模块',
                           ToolResult(ok=False, error='UNKNOWN_MODULE',
                                      message='未知模块' + _args_hint(spec, ctx)),
                           started)
        # C1：每次调用实时校验，不缓存任何权限判定
        if not has_perm(ctx.user, catalog.perm_of(catalog.MODULE_MAP[module_key]), 'read'):
            return _record(ctx, spec, args, False, '无权访问该模块',
                           ToolResult(ok=False, error='PERM_DENIED',
                                      message='当前账号无该模块访问权限'),
                           started)
        if not security.cloud_allowed(module_key, pol) and not ctx.is_local:
            return _record(ctx, spec, args, False, '该模块禁止外发云端模型',
                           ToolResult(ok=False, error='EGRESS_DENIED',
                                      message='该模块数据不允许发送给云端模型'),
                           started)

    try:
        result = spec.handler(ctx, **args)
    except Exception:  # noqa: BLE001 - 工具异常不能打断整个问答
        logger.exception('Copilot 工具执行异常：%s', spec.name)
        result = ToolResult(ok=False, error='TOOL_ERROR', message='查询失败，请稍后重试')

    return _record(ctx, spec, args, result.ok, '' if result.ok else result.message[:100],
                   result, started, row_count=result.row_count)


def _record(ctx, spec, args, permitted, reason, result, started, row_count=0):
    latency = int((time.time() - started) * 1000)
    try:
        CopilotToolCall.objects.create(
            run=ctx.run, conversation=ctx.conversation,
            tool_name=spec.name,
            args=args if isinstance(args, dict) else {'raw': str(args)[:200]},
            permitted=permitted, denied_reason=reason,
            row_count=row_count or 0, latency_ms=latency,
            result_digest=_digest(result.data) if result.ok else '',
            truncated=bool(result.truncated),
        )
    except Exception:  # noqa: BLE001 - 留痕失败不影响结果返回
        logger.exception('Copilot 工具留痕写入失败：%s', spec.name)
    return result
