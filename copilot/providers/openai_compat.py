"""任意 OpenAI 兼容网关（POST /chat/completions）。"""

from .base import BaseProvider, ChatResult, ProviderError, parse_openai_tool_calls


class OpenAICompatibleProvider(BaseProvider):
    name = 'openai_compatible'
    chat_path = '/chat/completions'
    supports_stream = True

    def chat(self, messages, temperature=0.2, max_tokens=None, timeout=60,
             tools=None, json_mode=False):
        payload = {
            'model': self.config.model,
            'messages': messages,
            'temperature': temperature,
            'max_tokens': max_tokens or self.config.max_tokens,
        }
        if tools:
            payload['tools'] = tools
        if json_mode:
            payload['response_format'] = {'type': 'json_object'}

        data = self._post(payload, timeout)
        try:
            choice = data['choices'][0]
            message = choice['message']
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f'网关响应格式无法解析：{exc}', 'BAD_RESPONSE') from exc

        content = message.get('content') or ''
        raw_calls = message.get('tool_calls') or []
        tool_calls = parse_openai_tool_calls(raw_calls)
        if not content.strip() and not tool_calls:
            raise ProviderError('模型返回内容为空', 'EMPTY')

        usage = data.get('usage') or {}
        return ChatResult(
            content=str(content).strip(),
            tool_calls=tool_calls,
            raw_tool_calls=raw_calls,
            prompt_tokens=int(usage.get('prompt_tokens') or 0),
            completion_tokens=int(usage.get('completion_tokens') or 0),
            finish_reason=str(choice.get('finish_reason') or ''),
        )
