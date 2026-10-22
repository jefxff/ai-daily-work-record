"""AI 润色客户端：按当前生效配置调用远端模型。

统一支持三类接入方式：
- ``ollama``         本地 Ollama（POST {base}/api/chat）；
- ``minimax``        MiniMax 对话补全（POST {base}/v1/text/chatcompletion_v2）；
- ``openai_compatible`` 任意 OpenAI 兼容网关（POST {base}/chat/completions）。

设计原则：任何外部调用失败都收敛为 ``AICallError``，由调用方回退到模板文本，
保证「AI 不可用」不影响周报 / 年度总结的正常可用性。
"""

import logging
from urllib.parse import urlsplit, urlunsplit

import requests

from .models import AiProviderConfig

logger = logging.getLogger(__name__)

OLLAMA_CHAT_PATH = "/api/chat"
MINIMAX_CHAT_PATH = "/v1/text/chatcompletion_v2"
OPENAI_CHAT_PATH = "/chat/completions"


class AICallError(Exception):
    """AI 调用失败（网络 / 鉴权 / 响应格式等）。"""


def _endpoint(config):
    """按提供商拼出实际请求地址；已填完整路径则原样使用。

    地址栏允许携带查询参数（如 MiniMax 的 ``?GroupId=xxx``），拼接路径时
    保留原有 query，避免把路径拼到 ``?`` 之后生成非法 URL。
    """
    base = config.effective_base_url().strip()
    if not base:
        raise AICallError("未配置 API 地址")
    if config.provider == "ollama":
        chat_path = OLLAMA_CHAT_PATH
    elif config.provider == "minimax":
        chat_path = MINIMAX_CHAT_PATH
    else:
        chat_path = OPENAI_CHAT_PATH
    parts = urlsplit(base)
    path = parts.path.rstrip("/")
    if not path.endswith(chat_path):
        path += chat_path
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def _extract_content(provider, data):
    try:
        if provider == "ollama":
            return data["message"]["content"]
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AICallError(f"AI 响应格式无法解析：{exc}") from exc


def chat(messages, config):
    """按配置调用一次对话补全，返回纯文本；失败抛 AICallError。"""
    if config is None:
        raise AICallError("尚未配置 AI 接入信息")

    url = _endpoint(config)
    headers = {"Content-Type": "application/json"}
    if config.api_key_plain:
        headers["Authorization"] = f"Bearer {config.api_key_plain}"

    if config.provider == "ollama":
        # Ollama 用 options 传采样参数，且不接收 max_tokens 顶层字段
        payload = {
            "model": config.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": config.temperature,
                "num_predict": config.max_tokens,
            },
        }
    else:
        payload = {
            "model": config.model,
            "messages": messages,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
        }

    try:
        resp = requests.post(url, json=payload, headers=headers,
                             timeout=config.timeout or 30)
    except requests.RequestException as exc:
        raise AICallError(f"请求失败：{exc}") from exc

    if resp.status_code >= 400:
        raise AICallError(f"HTTP {resp.status_code}：{resp.text[:200]}")

    try:
        data = resp.json()
    except ValueError as exc:
        raise AICallError("响应不是合法 JSON") from exc

    if config.provider == "minimax":
        # MiniMax 业务错误放在 base_resp 里（HTTP 仍是 200），必须透出真实原因，
        # 否则会落到 choices 解析失败，用户只看到「响应格式无法解析」。
        base_resp = data.get("base_resp") or {}
        status_code = base_resp.get("status_code")
        if status_code not in (None, 0):
            raise AICallError(
                f"MiniMax 返回错误 {status_code}：{base_resp.get('status_msg', '')}"
            )

    content = _extract_content(config.provider, data)
    if not content or not str(content).strip():
        raise AICallError("AI 返回内容为空")
    return str(content).strip()


def polish_text(text, instruction, config=None):
    """对文本做 AI 润色。

    未启用配置、未填模型或调用失败一律返回 None，由调用方回退模板文本。
    """
    config = config if config is not None else AiProviderConfig.current()
    if config is None or not config.is_enabled:
        return None
    if not (config.model or "").strip():
        logger.warning("AI 配置已启用但未填写模型名称，跳过润色")
        return None
    try:
        return chat(
            [
                {"role": "system", "content": instruction},
                {"role": "user", "content": text},
            ],
            config,
        )
    except AICallError as exc:
        logger.warning("AI 润色失败，回退模板文本：%s", exc)
        return None
    except Exception:
        logger.exception("AI 润色出现未预期异常，回退模板文本")
        return None


def test_connection(config):
    """连通性测试：返回 (是否成功, 说明文本)；不修改任何配置。"""
    try:
        reply = chat([{"role": "user", "content": "请回复两个字：可用"}], config)
        return True, f"连接成功，模型返回：{reply[:50]}"
    except AICallError as exc:
        return False, str(exc)
