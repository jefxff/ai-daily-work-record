"""模型接入层测试：三协议解析、地址校验。"""

from unittest.mock import patch

from django.test import SimpleTestCase

from copilot.providers.base import ChatResult, ProviderError, join_endpoint
from copilot.providers.minimax import MiniMaxProvider
from copilot.providers.ollama import OllamaProvider
from copilot.providers.openai_compat import OpenAICompatibleProvider


class FakeConfig:
    provider = 'ollama'
    model = 'qwen2.5:7b'
    api_key_plain = 'k'
    max_tokens = 512
    base_url = 'http://127.0.0.1:11434'

    def effective_base_url(self):
        return self.base_url

    def effective_timeout(self):
        return 30


class FakeResponse:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status
        self.text = str(data)

    def json(self):
        return self._data


class EndpointTests(SimpleTestCase):
    def test_preserves_query_string(self):
        url = join_endpoint('https://api.minimax.chat?GroupId=g1',
                            '/v1/text/chatcompletion_v2')
        self.assertEqual(url, 'https://api.minimax.chat/v1/text/chatcompletion_v2?GroupId=g1')

    def test_rejects_non_http(self):
        with self.assertRaises(ProviderError):
            join_endpoint('file:///etc/passwd', '/api/chat')

    def test_rejects_metadata_host(self):
        with self.assertRaises(ProviderError):
            join_endpoint('http://169.254.169.254', '/api/chat')


class OllamaTests(SimpleTestCase):
    @patch('copilot.providers.base.requests.post')
    def test_parses_content_and_tools(self, post):
        post.return_value = FakeResponse({
            'message': {
                'content': '',
                'tool_calls': [{'id': 'c1', 'function': {
                    'name': 'query_records', 'arguments': '{"module":"complaint"}'}}],
            },
            'prompt_eval_count': 12, 'eval_count': 7, 'done_reason': 'stop',
        })
        result = OllamaProvider(FakeConfig()).chat([{'role': 'user', 'content': 'hi'}])
        self.assertIsInstance(result, ChatResult)
        self.assertEqual(result.tool_calls[0]['name'], 'query_records')
        self.assertEqual(result.tool_calls[0]['arguments'], {'module': 'complaint'})
        self.assertEqual(result.prompt_tokens, 12)
        self.assertEqual(result.raw_tool_calls[0]['id'], 'c1')

    @patch('copilot.providers.base.requests.post')
    def test_error_body_raises(self, post):
        post.return_value = FakeResponse({'error': 'model not found'})
        with self.assertRaises(ProviderError):
            OllamaProvider(FakeConfig()).chat([{'role': 'user', 'content': 'hi'}])

    @patch('copilot.providers.base.requests.post')
    def test_empty_content_raises(self, post):
        post.return_value = FakeResponse({'message': {'content': '   '}})
        with self.assertRaises(ProviderError):
            OllamaProvider(FakeConfig()).chat([{'role': 'user', 'content': 'hi'}])


class MiniMaxTests(SimpleTestCase):
    def _config(self):
        config = FakeConfig()
        config.provider = 'minimax'
        config.base_url = 'https://api.minimax.chat'
        return config

    @patch('copilot.providers.base.requests.post')
    def test_base_resp_error_is_surfaced(self, post):
        post.return_value = FakeResponse({
            'base_resp': {'status_code': 1004, 'status_msg': 'invalid api key'},
        })
        with self.assertRaises(ProviderError) as ctx:
            MiniMaxProvider(self._config()).chat([{'role': 'user', 'content': 'hi'}])
        self.assertIn('1004', str(ctx.exception))

    @patch('copilot.providers.base.requests.post')
    def test_usage_tokens(self, post):
        post.return_value = FakeResponse({
            'base_resp': {'status_code': 0},
            'choices': [{'message': {'content': '好的'}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 9, 'completion_tokens': 4},
        })
        result = MiniMaxProvider(self._config()).chat([{'role': 'user', 'content': 'hi'}])
        self.assertEqual(result.content, '好的')
        self.assertEqual(result.total_tokens, 13)


class OpenAICompatTests(SimpleTestCase):
    def _config(self):
        config = FakeConfig()
        config.provider = 'openai_compatible'
        config.base_url = 'https://gw.example.com/v1'
        return config

    @patch('copilot.providers.base.requests.post')
    def test_parses_choice(self, post):
        post.return_value = FakeResponse({
            'choices': [{'message': {'content': '答案'}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 3, 'completion_tokens': 2},
        })
        result = OpenAICompatibleProvider(self._config()).chat(
            [{'role': 'user', 'content': 'hi'}])
        self.assertEqual(result.content, '答案')
        self.assertEqual(result.completion_tokens, 2)

    @patch('copilot.providers.base.requests.post')
    def test_http_error(self, post):
        post.return_value = FakeResponse({'message': 'bad'}, status=401)
        with self.assertRaises(ProviderError) as ctx:
            OpenAICompatibleProvider(self._config()).chat(
                [{'role': 'user', 'content': 'hi'}])
        self.assertEqual(ctx.exception.code, 'HTTP_ERROR')
