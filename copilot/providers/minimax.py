"""MiniMax 对话补全（POST /v1/text/chatcompletion_v2）。

MiniMax 的业务错误放在 ``base_resp`` 里且 HTTP 仍是 200，必须显式透出，
否则会落到 choices 解析失败，用户只看到"响应格式无法解析"。
"""

from .base import BaseProvider, ChatResult, ProviderError, parse_openai_tool_calls


class MiniMaxProvider(BaseProvider):
    name = 'minimax'
    chat_path = '/v1/text/chatcompletion_v2'
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

        data = self._post(payload, timeout)

        base_resp = (data or {}).get('base_resp') or {}
        status_code = base_resp.get('status_code')
        if status_code not in (None, 0):
            raise ProviderError(
                f'MiniMax 返回错误 {status_code}：{base_resp.get("status_msg", "")}',
                'MODEL_ERROR',
            )

        try:
            message = data['choices'][0]['message']
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f'MiniMax 响应格式无法解析：{exc}', 'BAD_RESPONSE') from exc

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
            finish_reason=str(data.get('choices', [{}])[0].get('finish_reason') or ''),
        )
