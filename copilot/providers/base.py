"""Provider 协议与公共工具。"""

import json
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

import requests


class ProviderError(Exception):
    """模型调用失败（网络 / 鉴权 / 响应格式 / 配置不合法）。"""

    def __init__(self, message, code='PROVIDER_ERROR'):
        super().__init__(message)
        self.code = code


@dataclass
class ChatResult:
    content: str = ''
    tool_calls: list = field(default_factory=list)   # [{'id','name','arguments'}]
    raw_tool_calls: list = field(default_factory=list)  # 原始结构，回填 assistant 消息用
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: str = ''

    @property
    def total_tokens(self):
        return (self.prompt_tokens or 0) + (self.completion_tokens or 0)


def join_endpoint(base_url, chat_path):
    """把 base_url 与对话路径拼接，保留原有 query（MiniMax 的 GroupId 等）。"""
    base = (base_url or '').strip()
    if not base:
        raise ProviderError('未配置 API 地址', 'NO_BASE_URL')
    parts = urlsplit(base)
    if parts.scheme not in ('http', 'https'):
        raise ProviderError('API 地址必须是 http/https', 'BAD_BASE_URL')
    host = (parts.hostname or '').lower()
    # 云元数据地址：绝不允许（管理员误配即等于把内网凭据暴露给外部服务）
    if host in ('169.254.169.254', 'metadata.google.internal'):
        raise ProviderError('API 地址不允许指向元数据服务', 'BAD_BASE_URL')
    path = parts.path.rstrip('/')
    if not path.endswith(chat_path):
        path += chat_path
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


class BaseProvider:
    name = 'base'
    chat_path = ''
    supports_tools = False
    supports_stream = False

    def __init__(self, config):
        self.config = config

    @property
    def endpoint(self):
        return join_endpoint(self.config.effective_base_url(), self.chat_path)

    @property
    def headers(self):
        headers = {'Content-Type': 'application/json'}
        key = self.config.api_key_plain
        if key:
            headers['Authorization'] = f'Bearer {key}'
        return headers

    def chat(self, messages, temperature=0.2, max_tokens=None, timeout=60,
             tools=None, json_mode=False):
        raise NotImplementedError

    def _post(self, payload, timeout):
        try:
            resp = requests.post(self.endpoint, json=payload, headers=self.headers,
                                 timeout=timeout)  # nosec B113 - timeout 显式传入
        except requests.RequestException as exc:
            raise ProviderError(f'请求失败：{exc}', 'NETWORK') from exc
        if resp.status_code >= 400:
            raise ProviderError(f'HTTP {resp.status_code}：{resp.text[:200]}', 'HTTP_ERROR')
        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderError('响应不是合法 JSON', 'BAD_RESPONSE') from exc


def parse_openai_tool_calls(raw_calls):
    """把 OpenAI 风格的 tool_calls 归一成 [{name, arguments}]。

    arguments 可能是 JSON 字符串（多数网关）或已经是对象（部分本地实现），
    两种都要兼容；解析失败留空 dict，由上层参数校验报错，避免整个请求失败。
    """
    result = []
    for call in raw_calls or []:
        func = (call or {}).get('function') or {}
        name = func.get('name') or ''
        args = func.get('arguments')
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except ValueError:
                args = {}
        result.append({
            'id': (call or {}).get('id', ''),
            'name': name,
            'arguments': args if isinstance(args, dict) else {},
        })
    return result
