"""解析器测试：本地小模型输出的各种"脏格式"。"""

from django.test import SimpleTestCase

from copilot.engine.parser import extract_json, parse_decision

TOOLS = {'query_records', 'aggregate_records'}


class ExtractJsonTests(SimpleTestCase):
    def test_plain(self):
        self.assertEqual(extract_json('{"a": 1}'), {'a': 1})

    def test_fenced(self):
        text = '```json\n{"tool": "query_records"}\n```'
        self.assertEqual(extract_json(text), {'tool': 'query_records'})

    def test_surrounded_by_prose(self):
        text = '好的，我来查一下：{"tool":"query_records","args":{}} 请稍等'
        self.assertIn('tool', extract_json(text))

    def test_trailing_comma(self):
        self.assertEqual(extract_json('{"a":1,}'), {'a': 1})

    def test_single_quotes(self):
        self.assertEqual(extract_json("{'a': 1}"), {'a': 1})

    def test_not_json(self):
        self.assertIsNone(extract_json('这只是普通文本'))


class ParseDecisionTests(SimpleTestCase):
    def test_tool_decision(self):
        decision = parse_decision(
            '{"tool":"query_records","args":{"module":"complaint"}}', TOOLS)
        self.assertEqual(decision['type'], 'tool')
        self.assertEqual(decision['name'], 'query_records')
        self.assertEqual(decision['args']['module'], 'complaint')

    def test_arguments_as_json_string(self):
        decision = parse_decision(
            '{"name":"query_records","arguments":"{\\"module\\":\\"complaint\\"}"}', TOOLS)
        self.assertEqual(decision['args'], {'module': 'complaint'})

    def test_unknown_tool_marked_unsupported(self):
        decision = parse_decision('{"tool":"drop_table","args":{}}', TOOLS)
        self.assertEqual(decision['type'], 'unsupported')
        self.assertEqual(decision['name'], 'drop_table')

    def test_business_json_not_mistaken_for_tool(self):
        # 没有 tool/action 键、只有 name 的业务对象，不能当成工具调用
        decision = parse_decision('{"name":"东方新材料有限公司","amount":100}', TOOLS)
        self.assertEqual(decision['type'], 'final')

    def test_answer_wrapped_json(self):
        decision = parse_decision('{"answer":"共 3 条记录"}', TOOLS)
        self.assertEqual(decision['type'], 'final')
        self.assertEqual(decision['content'], '共 3 条记录')

    def test_plain_text_is_final(self):
        decision = parse_decision('上个月共 3 条客诉。', TOOLS)
        self.assertEqual(decision['type'], 'final')
        self.assertEqual(decision['content'], '上个月共 3 条客诉。')

    def test_empty_output(self):
        decision = parse_decision('', TOOLS)
        self.assertEqual(decision['type'], 'final')
        self.assertEqual(decision['content'], '')
