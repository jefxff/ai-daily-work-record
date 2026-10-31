"""Copilot 安全层：数据分级、外发策略、脱敏、限流。

三个不变量：
1. **L3 敏感模块默认不出内网**——只有 ``is_local=True`` 的模型才允许；
   云端模型需管理员在策略里逐模块放行（``CopilotPolicy.allow_cloud_modules``）。
2. **L4 永不进入工具层**——账号、备份、系统配置不在 catalog 里，无法被工具选中。
3. **脱敏在工具返回值上做，不在提示词里做**——模型压根看不到完整值，
   避免"提示词要求它别说"这种不可验证的约束。
"""

import logging
import re
import time
from dataclasses import dataclass

from django.core.cache import cache
from django.utils import timezone

from .catalog import LEVEL_LABELS, MODULE_MAP  # noqa: F401  (LEVEL_LABELS 供模板/接口使用)
from .models import CopilotPolicy

logger = logging.getLogger(__name__)

# 永远不允许外发云端的级别
CLOUD_BLOCKED_LEVELS = {'L3', 'L4'}

_ID_CARD_RE = re.compile(r'(?<!\d)\d{17}[\dXx](?!\d)')
_PHONE_RE = re.compile(r'(?<!\d)1[3-9]\d{9}(?!\d)')
_EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+\.[\w.]+')


def policy():
    return CopilotPolicy.current()


def module_level(module_key):
    spec = MODULE_MAP.get(module_key)
    return spec.level if spec else 'L4'


def cloud_allowed(module_key, pol=None):
    """该模块的数据是否允许发往云端模型。"""
    spec = MODULE_MAP.get(module_key)
    if spec is None:
        return False
    if spec.level not in CLOUD_BLOCKED_LEVELS:
        return True
    pol = pol or policy()
    return pol.cloud_allowed(module_key)


def spec_allowed_for_egress(spec, is_local, pol=None):
    if is_local:
        return True
    return cloud_allowed(spec.key, pol)


def _keep(text, head, tail):
    """保留首尾、中间打码；长度不足时整体打码。"""
    if len(text) <= head + tail:
        return '*' * max(3, len(text))
    return f'{text[:head]}{"*" * max(3, len(text) - head - tail)}{text[-tail:]}'


def _rule_for(field_name):
    name = (field_name or '').lower()
    if 'id_card' in name or 'idcard' in name:
        return 'id_card'
    if 'phone' in name or 'mobile' in name or name in {'tel', 'telephone'}:
        return 'phone'
    if 'email' in name or 'mail' in name:
        return 'email'
    if 'bank' in name or 'account_no' in name:
        return 'bank'
    if name in {'order_number', 'policy_no', 'policy_number'}:
        return 'tail4'
    return ''


def mask_value(field_name, value, rule=None):
    """按字段名规则脱敏单个值；无法识别的返回原值。"""
    if value is None or value == '':
        return value
    text = str(value)
    rule = rule or _rule_for(field_name)
    if rule == 'id_card':
        return _keep(text, 6, 4)
    if rule == 'phone':
        return _keep(text, 3, 4)
    if rule == 'email':
        if '@' in text:
            local, domain = text.split('@', 1)
            return f'{local[:1]}***@{domain}'
        return _keep(text, 1, 0)
    if rule == 'bank':
        return _keep(text, 0, 4)
    if rule == 'tail4':
        return _keep(text, 0, 4)
    return value


def mask_text(text):
    """自由文本兜底脱敏：手机号 / 身份证 / 邮箱。

    备注、客户名称等长文本里可能夹带个人信息，字段级规则覆盖不到，
    这里统一扫一遍。只做遮罩，不改变文本结构。
    """
    if not text:
        return text
    text = _ID_CARD_RE.sub(lambda m: _keep(m.group(0), 6, 4), text)
    text = _PHONE_RE.sub(lambda m: _keep(m.group(0), 3, 4), text)
    text = _EMAIL_RE.sub(lambda m: mask_value('email', m.group(0), 'email'), text)
    return text


def mask_row(spec, row, enabled=True):
    """对一行数据脱敏，返回 (新行, 已脱敏字段名列表)。"""
    if not enabled:
        return row, []
    masked_fields = []
    out = {}
    for key, value in row.items():
        if isinstance(value, str):
            if key in (spec.sensitive_fields or ()) or _rule_for(key):
                new_value = mask_value(key, value)
                if new_value != value:
                    masked_fields.append(key)
                out[key] = new_value
            else:
                new_value = mask_text(value)
                if new_value != value:
                    masked_fields.append(key)
                out[key] = new_value
        else:
            out[key] = value
    return out, masked_fields


@dataclass
class RateVerdict:
    allowed: bool
    retry_after: int = 0
    limit: int = 0


def check_rate_limit(user, pol=None):
    """双窗口限流：10 分钟 + 每日。

    缓存不可用时**放行**（记 warning）：限流是成本控制手段，
    不能因为缓存表缺失就阻断业务请求。
    """
    pol = pol or policy()
    now = int(time.time())
    windows = (
        (f'copilot:rl:10m:{user.pk}:{now // 600}', 600, pol.rate_limit_per_10min),
        (f'copilot:rl:day:{user.pk}:{timezone.localdate().isoformat()}',
         86400, pol.rate_limit_per_day),
    )
    for key, ttl, limit in windows:
        if not limit or limit <= 0:
            continue
        try:
            cache.add(key, 0, ttl)
            count = cache.incr(key)
        except Exception:  # noqa: BLE001 - 缓存不可用不应影响业务
            logger.warning('限流计数失败（缓存不可用），放行本次请求', exc_info=False)
            continue
        if count > limit:
            return RateVerdict(False, ttl, limit)
    return RateVerdict(True)


def sanitize_answer(text, allowed_urls):
    """清掉模型自己编的站内链接。

    引用卡片由服务端注入；正文里若出现 markdown 链接且目标不在白名单，
    降级为纯文本（保留链接文字），防止模型凭空构造 /records/xxx/1/ 指向
    别人的数据。
    """
    if not text:
        return text
    allowed = set(allowed_urls or ())

    def _repl(match):
        label, url = match.group(1), match.group(2)
        return f'{label}（见下方引用）' if url not in allowed else match.group(0)

    return re.sub(r'\[([^\]]{1,80})\]\((/[^)\s]{1,300})\)', _repl, text)
