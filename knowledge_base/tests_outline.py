"""幕布化 P0 验收：Markdown ↔ 树双向转换（knowledge_base.outline）。

验收口径（方案 03 P0）：
- md → tree → md 幂等（第二轮起逐字节稳定）；
- 各语法成分（标题/待办/引用/代码块/分隔线/嵌套/松散列表/中文）不丢不乱；
- 坏输入不抛异常。
"""
import re

from django.contrib.auth.models import User
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from .models import KnowledgeDocument, KnowledgeNode
from .outline import count_nodes, depth_of, md_to_tree, tree_to_md

SAMPLE = """# 项目说明

## 背景

客户报价流程太散，**需要归档**。

- [x] 梳理现有流程
- [ ] 补充附件模板
- 一级条目
    - 四空格缩进子条目
- [ ] 带备注的待办

> 引用第一行
> 引用第二行

```python
def hello():
    return "world"
```

---

1. 有序列表甲
2. 有序列表乙
* 星号列表

表格如下：
| 列一 | 列二 |
| --- | --- |
| A | B |

图片行：![截图](/media/a.png)
内联标记：`code` 与 [链接](http://e.com) 与 [[doc:12|别名]]
"""


def _strip_md(text):
    """文本等价比较：去掉列表符号/缩进/标记，只留语义行。"""
    lines = []
    for line in text.split('\n'):
        s = line.strip()
        s = re.sub(r'^(?:[-*]|\d+[.)])\s+', '', s)
        s = re.sub(r'^#{1,6}\s+', '', s)
        if s:
            lines.append(s)
    return lines


class OutlineRoundTripTests(SimpleTestCase):
    def test_round_trip_idempotent(self):
        """md → tree → md → tree → md：第二轮起逐字节稳定。"""
        md1 = tree_to_md(md_to_tree(SAMPLE))
        md2 = tree_to_md(md_to_tree(md1))
        self.assertEqual(md1, md2)

    def test_text_equivalence(self):
        """原文所有语义行都在转换结果中出现（不丢内容）。"""
        md1 = tree_to_md(md_to_tree(SAMPLE))
        got = _strip_md(md1)
        for want in _strip_md(SAMPLE):
            self.assertIn(want, got, f'丢失内容行: {want}')

    def test_todo_nodes(self):
        tree = md_to_tree('- [ ] 未完成事项\n- [x] 已完成事项')
        todos = [n for n in tree if n['type'] == 'todo']
        self.assertEqual(len(todos), 2)
        self.assertFalse(todos[0]['done'])
        self.assertTrue(todos[1]['done'])
        self.assertEqual(todos[0]['text'], '未完成事项')

    def test_nesting_with_indent_units(self):
        # 2 空格 / 4 空格 / Tab 三种缩进都能正确成层级
        for indent in ('  ', '    ', '\t'):
            src = f'- 甲\n{indent}- 乙\n{indent}{indent}- 丙'
            tree = md_to_tree(src)
            self.assertEqual(len(tree), 1)
            child = tree[0]['children'][0]
            self.assertEqual(child['text'], '乙')
            self.assertEqual(child['children'][0]['text'], '丙')

    def test_loose_list_keeps_structure(self):
        """条目间有空行（松散列表）不破坏层级。"""
        src = '- 甲\n\n  - 乙\n\n  - 丙\n- 丁'
        tree = md_to_tree(src)
        self.assertEqual(len(tree), 2)
        self.assertEqual([c['text'] for c in tree[0]['children']], ['乙', '丙'])

    def test_headings_and_levels(self):
        tree = md_to_tree('# 一级\n## 二级\n### 三级\n#### 四级按三级处理')
        self.assertEqual([n['type'] for n in tree], ['heading'] * 4)
        self.assertEqual([n['heading_level'] for n in tree], [1, 2, 3, 3])

    def test_quote_per_line(self):
        tree = md_to_tree('> 甲\n> 乙')
        self.assertEqual(len(tree), 2)
        self.assertEqual(tree[0]['type'], 'quote')
        self.assertEqual(tree[0]['text'], '甲')

    def test_code_fence_whole_block(self):
        tree = md_to_tree('```python\nline1\nline2\n```')
        self.assertEqual(len(tree), 1)
        node = tree[0]
        self.assertEqual(node['type'], 'code')
        self.assertEqual(node['text'], 'python')
        self.assertEqual(node['note'], 'line1\nline2')
        # 反向还原完整代码块
        md1 = tree_to_md(tree)
        self.assertEqual(md_to_tree(md1)[0]['note'], 'line1\nline2')

    def test_divider(self):
        tree = md_to_tree('甲\n---\n乙')
        self.assertEqual(tree[1]['type'], 'divider')

    def test_ordered_list_normalized(self):
        """有序列表按 text 节点处理，序号在转换中被统一。"""
        tree = md_to_tree('1. 甲\n2. 乙')
        self.assertEqual([n['text'] for n in tree], ['甲', '乙'])

    def test_plain_paragraph_becomes_text_nodes(self):
        tree = md_to_tree('普通段落一\n普通段落二')
        self.assertEqual([n['type'] for n in tree], ['text', 'text'])
        self.assertEqual(tree[0]['text'], '普通段落一')

    def test_empty_and_bad_input(self):
        self.assertEqual(md_to_tree(''), [])
        self.assertEqual(md_to_tree(None), [])
        self.assertEqual(md_to_tree('\n\n\n'), [])
        self.assertEqual(tree_to_md(None), '')
        self.assertEqual(tree_to_md([]), '')

    def test_count_and_depth(self):
        tree = md_to_tree('- 甲\n  - 乙\n    - 丙\n- 丁')
        self.assertEqual(count_nodes(tree), 4)
        self.assertEqual(depth_of(tree), 3)
        self.assertEqual(count_nodes([]), 0)
        self.assertEqual(depth_of([]), 0)

    def test_cjk_and_inline_marks_preserved(self):
        src = '- **加粗**与`行内代码`与中文混排 ✅'
        node = md_to_tree(src)[0]
        self.assertEqual(node['text'], '**加粗**与`行内代码`与中文混排 ✅')
        self.assertIn('**加粗**', tree_to_md(md_to_tree(src)))


class ApiDocTreeTests(TestCase):
    """P1 只读大纲/导图数据源：可见性必须与详情页同口径（404 不泄露存在性）。"""

    PWD = 'X12345678'

    def setUp(self):
        from .tests import grant
        self.alice = User.objects.create_user('ot_alice', 'a@e.com', self.PWD, is_active=True)
        self.bob = User.objects.create_user('ot_bob', 'b@e.com', self.PWD, is_active=True)
        grant(self.alice, ('knowledge', 'read'))
        grant(self.bob, ('knowledge', 'read'))
        self.public_doc = KnowledgeDocument.objects.create(
            title='公开文档', content='# 标题\n- 甲\n- [ ] 乙',
            status='published', owner=self.alice, visibility='public')
        self.private_doc = KnowledgeDocument.objects.create(
            title='Alice私人', content='秘密', status='published',
            owner=self.alice, visibility='private')
        self.client = Client()

    def _login(self, user):
        c = Client()
        c.force_login(user)
        return c

    def test_markdown_doc_parsed_on_the_fly(self):
        c = self._login(self.alice)
        resp = c.get(reverse('knowledge_base:api_doc_tree', args=[self.public_doc.pk]))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['editor_mode'], 'markdown')
        self.assertEqual([n['type'] for n in data['tree']], ['heading', 'text', 'todo'])
        # 即时解析不落库：仍是 markdown 模式、节点表为空（P1 零数据变更）
        self.public_doc.refresh_from_db()
        self.assertEqual(self.public_doc.editor_mode, 'markdown')
        self.assertFalse(KnowledgeNode.objects.exists())

    def test_private_doc_hidden_from_others(self):
        resp = self._login(self.bob).get(
            reverse('knowledge_base:api_doc_tree', args=[self.private_doc.pk]))
        self.assertEqual(resp.status_code, 404)

    def test_private_doc_visible_to_owner(self):
        resp = self._login(self.alice).get(
            reverse('knowledge_base:api_doc_tree', args=[self.private_doc.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_anonymous_redirects_to_login(self):
        resp = self.client.get(
            reverse('knowledge_base:api_doc_tree', args=[self.public_doc.pk]))
        self.assertEqual(resp.status_code, 302)

    def test_outline_doc_assembled_from_nodes(self):
        self.public_doc.editor_mode = 'outline'
        self.public_doc.save(update_fields=['editor_mode'])
        root = KnowledgeNode.objects.create(
            document=self.public_doc, uid='aaa111bbb222', sort=0,
            text='根节点', node_type='todo', is_done=False)
        child = KnowledgeNode.objects.create(
            document=self.public_doc, uid='ccc333ddd444', parent=root, sort=0,
            text='子节点', node_type='text')
        resp = self._login(self.alice).get(
            reverse('knowledge_base:api_doc_tree', args=[self.public_doc.pk]))
        data = resp.json()
        self.assertEqual(data['editor_mode'], 'outline')
        self.assertEqual(len(data['tree']), 1)
        node = data['tree'][0]
        self.assertEqual(node['uid'], root.uid)
        self.assertEqual(node['type'], 'todo')
        self.assertEqual(node['children'][0]['uid'], child.uid)
