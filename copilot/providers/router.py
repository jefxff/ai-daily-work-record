"""模型网关：按用途选配置、本地优先、熔断、失败降级。

选择顺序：``AiProviderConfig.enabled_candidates(purpose)`` 已按"本地优先、
新配置优先"排好。一次 run 内**不中途换模型**——换模型会导致可用工具集
（受外发策略影响）发生变化，中途切换会让工具清单与已生成的调用对不上。
失败时直接降级到关键词搜索，比"偷偷换云端模型"更可预测。
"""

import logging

from django.core.cache import cache

from ai_api.models import AiProviderConfig

from .base import ChatResult, ProviderError
from .minimax import MiniMaxProvider
from .ollama import OllamaProvider
from .openai_compat import OpenAICompatibleProvider

logger = logging.getLogger(__name__)

PROVIDERS = {
    'ollama': OllamaProvider,
    'minimax': MiniMaxProvider,
    'openai_compatible': OpenAICompatibleProvider,
}

FAILURE_THRESHOLD = 5
COOLDOWN_SECONDS = 60
FAILURE_WINDOW = 3600


def build(config):
    provider_cls = PROVIDERS.get(config.provider)
    if provider_cls is None:
        raise ProviderError(f'不支持的提供方：{config.provider}', 'BAD_PROVIDER')
    return provider_cls(config)


def _breaker_key(config):
    return f'copilot:cb:{config.provider}:{config.model or "default"}'


def is_open(config):
    """熔断是否处于冷却期。缓存不可用时视为正常（不阻断业务）。"""
    try:
        return bool(cache.get(f'{_breaker_key(config)}:open'))
    except Exception:  # noqa: BLE001
        return False


def record_failure(config):
    try:
        counter = f'{_breaker_key(config)}:fail'
        cache.add(counter, 0, FAILURE_WINDOW)
        count = cache.incr(counter)
        if count >= FAILURE_THRESHOLD:
            cache.set(f'{_breaker_key(config)}:open', 1, COOLDOWN_SECONDS)
            logger.warning('模型熔断打开：%s/%s', config.provider, config.model)
    except Exception:  # noqa: BLE001
        pass


def record_success(config):
    try:
        cache.delete(f'{_breaker_key(config)}:fail')
        cache.delete(f'{_breaker_key(config)}:open')
    except Exception:  # noqa: BLE001
        pass


def candidates(purpose):
    """某用途下的可用配置（已跳过熔断中的）。"""
    return [c for c in AiProviderConfig.enabled_candidates(purpose) if not is_open(c)]


def select(purpose):
    """选一个配置，返回 (config, provider)；没有可用配置返回 (None, None)。"""
    rows = candidates(purpose)
    if not rows:
        return None, None
    config = rows[0]
    if not (config.model or '').strip():
        raise ProviderError('AI 配置已启用但未填写模型名称', 'NO_MODEL')
    return config, build(config)


def chat(purpose, messages, temperature=0.2, max_tokens=None, tools=None,
         json_mode=False):
    """按用途调用模型，逐个候选重试；全部失败抛最后一个 ProviderError。"""
    rows = candidates(purpose)
    if not rows:
        raise ProviderError('没有可用的 AI 模型配置', 'NO_PROVIDER')

    last_error = None
    for config in rows:
        if not (config.model or '').strip():
            last_error = ProviderError('AI 配置已启用但未填写模型名称', 'NO_MODEL')
            continue
        try:
            return chat_with(config, messages, temperature=temperature,
                             max_tokens=max_tokens, tools=tools,
                             json_mode=json_mode), config
        except ProviderError as exc:
            last_error = exc
            continue

    raise last_error or ProviderError('模型调用失败', 'PROVIDER_ERROR')


def chat_with(config, messages, temperature=0.2, max_tokens=None, tools=None,
              json_mode=False, on_delta=None):
    """用指定配置调用一次（Caller 已选好模型，中途不换）。

    执行器用这个而不是 ``chat()``：一次问答内固定模型，工具集才不会
    因为"本地换成云端"而中途变化（敏感模块在两种模型下可用性不同）。

    ``on_delta`` 只在"要边生成边显示"时传（SSE）；不传就是原来的整段返回。
    """
    provider = build(config)
    try:
        result = provider.chat(
            messages,
            temperature=temperature,
            max_tokens=max_tokens or config.max_tokens,
            timeout=config.effective_timeout(),
            tools=tools,
            json_mode=json_mode,
            on_delta=on_delta,
        )
    except ProviderError:
        record_failure(config)
        raise
    record_success(config)
    return result
