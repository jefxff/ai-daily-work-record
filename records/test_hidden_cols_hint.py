"""「已隐藏 N 个次要列」提示条与实际列数的一致性守卫。

背景：列表页在窄屏隐藏带 ``.col-optional`` 的列，并在表格上方显示提示条。
2026-11-08 复核发现 11 处提示条里有 3 处数字与真实列数不符
（诉讼 3/实际 2、客户台账 4/实际 5、试样列表 1/实际 2），即「提示说谎」。
本测试用静态解析在 CI 中拦住「增删列后忘记改文案」，不依赖浏览器。

核价页用定性文案（「已隐藏部分次要列」）规避数字，属合法写法，跳过校验。
"""
import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

_HINT = re.compile(r'<div[^>]*hidden-cols-hint[^>]*>(.*?)</div>', re.S)
_TABLE = re.compile(r'<table\b.*?</table>', re.S)
_NUM = re.compile(r'(\d+)\s*个次要列')
_TH_OPTIONAL = re.compile(r'<th\b[^>]*col-optional', re.I)
_EXCLUDE = {'staticfiles', 'node_modules', '.ui-audit', '.git', '.shot', 'test-results'}


class HiddenColsHintTests(SimpleTestCase):
    def test_hint_number_matches_optional_columns(self):
        root = Path(settings.BASE_DIR)
        errors, checked = [], 0
        for p in sorted(root.rglob('*.html')):
            if any(part in _EXCLUDE for part in p.parts):
                continue
            text = p.read_text(encoding='utf-8', errors='ignore')
            if 'hidden-cols-hint' not in text:
                continue
            for m in _HINT.finditer(text):
                hint = re.sub(r'<[^>]+>', '', m.group(1)).strip()
                num = _NUM.search(hint)
                if not num:
                    continue
                table = _TABLE.search(text[m.end():])
                if not table:
                    errors.append(f'{p.name}: 提示条后找不到表格 -> {hint[:36]}')
                    continue
                actual = len(_TH_OPTIONAL.findall(table.group(0)))
                checked += 1
                if int(num.group(1)) != actual:
                    errors.append(f'{p.name}: 文案写 {num.group(1)} 个，实际 {actual} 个')
        self.assertGreaterEqual(checked, 10, '数字型提示条应至少覆盖 10 处')
        self.assertEqual(errors, [], '\n'.join(errors))
