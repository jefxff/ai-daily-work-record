"""任意 OpenAI 兼容网关（POST /chat/completions）。

流式（``on_delta``）只对**文本增量**负责：工具调用仍然在流式协议里累积
（``delta.tool_calls`` 按 index 拼 arguments），因为网关对工具调用的分块
方式不一致，一次性拼完再交给执行器更稳。
"""

import json

import requests

from .base import BaseProvider, ChatResult, ProviderError, parse_openai_tool_calls


def _merge_tool_delta(slots, chunk):
    """把流式 ``delta.tool_calls`` 按 index 累积成完整调用。"""
    for call in chunk or []:
        index = call.get('index', 0)
        slot = slots.setdefault(index, {
            'id': '', 'type': 'function',
            'function': {'name': '', 'arguments': ''},
        })
        if call.get('id'):
            slot['id'] = call['id']
        func = call.get('function') or {}
        if func.get('name'):
            # 少数网关把 name 也分块下发，拼起来比覆盖安全
            slot['function']['name'] += func['name']
        if func.get('arguments'):
            slot['function']['arguments'] += func['arguments']


class OpenAICompatibleProvider(BaseProvider):
    name = 'openai_compatible'
    chat_path = '/chat/completions'
    supports_stream = True

    def chat(self, messages, temperature=0.2, max_tokens=None, timeout=60,
             tools=None, json_mode=False, on_delta=None):
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

        if on_delta is not None:
            payload['stream'] = True
            return self._chat_stream(payload, timeout, on_delta)

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

    def _chat_stream(self, payload, timeout, on_delta):
        try:
            resp = requests.post(self.endpoint, json=payload, headers=self.headers,
                                 timeout=timeout, stream=True)  # nosec B113
        except requests.RequestException as exc:
            raise ProviderError(f'请求失败：{exc}', 'NETWORK') from exc
        if resp.status_code >= 400:
            raise ProviderError(f'HTTP {resp.status_code}：{resp.text[:200]}', 'HTTP_ERROR')

        parts, slots = [], {}
        prompt_tokens = completion_tokens = 0
        finish_reason = ''
        try:
            for raw_line in resp.iter_lines(decode_unicode=True):
                if not raw_line:
                    continue
                line = raw_line.strip() if isinstance(raw_line, str) else raw_line.decode('utf-8', 'ignore')
                if not line.startswith('data:'):
                    continue
                data = line[5:].strip()
                if data == '[DONE]':
                    break
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue
                usage = obj.get('usage') or {}
                if usage:
                    prompt_tokens = int(usage.get('prompt_tokens') or prompt_tokens)
                    completion_tokens = int(usage.get('completion_tokens') or completion_tokens)
                choices = obj.get('choices') or []
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get('delta') or {}
                piece = delta.get('content')
                if piece:
                    parts.append(piece)
                    try:
                        on_delta(piece)
                    except Exception:  # noqa: BLE001 - 回调故障不能打断模型调用
                        pass
                _merge_tool_delta(slots, delta.get('tool_calls'))
                if choice.get('finish_reason'):
                    finish_reason = str(choice['finish_reason'])
        except requests.RequestException as exc:
            # 已经吐了一部分内容：不要整段丢弃，交给上层按"不完整"处理
            if not parts and not slots:
                raise ProviderError(f'请求失败：{exc}', 'NETWORK') from exc
        finally:
            resp.close()

        raw_calls = [slots[k] for k in sorted(slots)]
        content = ''.join(parts).strip()
        tool_calls = parse_openai_tool_calls(raw_calls)
        if not content and not tool_calls:
            raise ProviderError('模型返回内容为空', 'EMPTY')
        if not completion_tokens:
            # 流式默认不带 usage：按字符数粗估，只用于预算控制，不用于计费
            completion_tokens = max(1, len(content) // 2) if content else 0
        return ChatResult(
            content=content,
            tool_calls=tool_calls,
            raw_tool_calls=raw_calls,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            finish_reason=finish_reason,
        )
