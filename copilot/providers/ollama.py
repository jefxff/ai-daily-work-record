"""本地 Ollama（POST /api/chat）。

注意与 OpenAI 的差异：
- 采样参数放在 ``options`` 里（temperature / num_predict），不接顶层 max_tokens；
- 用量字段是 prompt_eval_count / eval_count；
- 模型报错体是 ``{"error": "..."}``，HTTP 仍可能是 200。
"""

from .base import BaseProvider, ChatResult, ProviderError, parse_openai_tool_calls


class OllamaProvider(BaseProvider):
    name = 'ollama'
    chat_path = '/api/chat'
    supports_stream = True

    def chat(self, messages, temperature=0.2, max_tokens=None, timeout=60,
             tools=None, json_mode=False, on_delta=None):
        # 该协议暂未实现增量下发（on_delta 被忽略）：SSE 仍会推
        # 步骤与最终答案，只是没有逐字效果。
        payload = {
            'model': self.config.model,
            'messages': messages,
            'stream': False,
            'options': {
                'temperature': temperature,
                'num_predict': max_tokens or self.config.max_tokens,
            },
        }
        if tools:
            # Ollama 的 tools 结构与 OpenAI 一致；模型不支持时它会忽略或报错，
            # 上层根据 supports_tools 决定是否传入，这里只做透传。
            payload['tools'] = tools
        if json_mode:
            payload['format'] = 'json'

        data = self._post(payload, timeout)
        if isinstance(data, dict) and data.get('error'):
            raise ProviderError(f'Ollama 返回错误：{data["error"]}', 'MODEL_ERROR')

        message = (data or {}).get('message') or {}
        content = message.get('content') or ''
        raw_calls = message.get('tool_calls') or []
        tool_calls = parse_openai_tool_calls(raw_calls)
        if not content.strip() and not tool_calls:
            raise ProviderError('模型返回内容为空', 'EMPTY')
        return ChatResult(
            content=str(content).strip(),
            tool_calls=tool_calls,
            raw_tool_calls=raw_calls,
            prompt_tokens=int(data.get('prompt_eval_count') or 0),
            completion_tokens=int(data.get('eval_count') or 0),
            finish_reason=str(data.get('done_reason') or ''),
        )
