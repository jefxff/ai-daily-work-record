"""屏幕 UI 正文字号下限守卫（UI 审计 P1-4）。

整改批次曾在自研 CSS 里把 22 处 <12px 统一到 12px，却漏掉了模板内联
``<style>`` 里的同类声明，导致「<12px 文本实测 0」只在被抽查的首页成立。
本测试把口径写成机器可校验的规则：

* 适用范围：所有 ``*/templates/*.html`` 与 ``static/**/*.css``。
* 下限：12px（rem/em 按 16px 根字号折算，pt 按 4/3 折算）。
* 显式例外（必须在此登记，不能靠注释悄悄放过）：
  1. ``*print.html`` / ``*pdf.html``：打印与 PDF 排版用 pt，不按屏幕像素口径衡量；
  2. ``static/css/customer-review.css``：高密度核价/复核数据标签，
     统一到 12px 会破坏紧凑布局，见整改说明 §4；
  3. 第三方压缩 CSS（``*.min.css``）；
  4. ``font-size: 0``：视觉隐藏技巧，不渲染文字；
  5. 行内 ``ui-font-exempt`` 标记：SVG 画布固定坐标的图形标注，
     放大后会溢出图形边界（当前仅知识脑图折叠标记一处）。

例外之外新增小字号会在 CI 直接失败，从源头防止「抽查通过、全站回退」。
"""

import re
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parent.parent

# font-size: 0.72rem / .72rem / 11px / 8pt / 0
_DECL = re.compile(r"font-size\s*:\s*([0-9]*\.?[0-9]+)\s*(px|rem|em|pt)?\b", re.I)
_UNIT_PX = {"px": 1.0, "rem": 16.0, "em": 16.0, "pt": 4.0 / 3, None: 1.0}
_MIN_PX = 12.0
_EXEMPT_MARK = "ui-font-exempt"

# 例外 1：打印 / PDF 模板（文件名匹配）
_PRINT_TEMPLATE = re.compile(r"(print|pdf)\.html$", re.I)
# 例外 2：高密度数据标签模块（相对 ROOT 的 POSIX 路径）
_ALLOWLISTED_CSS = {"static/css/customer-review.css"}


def _targets():
    """产出 (相对路径, Path)，只包含需要接受字号守卫的文件。"""
    for path in sorted(ROOT.rglob("*.html")):
        rel = path.relative_to(ROOT).as_posix()
        if "/templates/" not in f"/{rel}" or "node_modules" in rel:
            continue
        if _PRINT_TEMPLATE.search(path.name):
            continue
        yield rel, path

    for path in sorted((ROOT / "static").rglob("*.css")):
        rel = path.relative_to(ROOT).as_posix()
        if path.name.endswith(".min.css") or rel in _ALLOWLISTED_CSS:
            continue
        yield rel, path


def _violations(rel, path):
    text = path.read_text(encoding="utf-8")
    for lineno, line in enumerate(text.splitlines(), 1):
        if _EXEMPT_MARK in line:
            continue
        for match in _DECL.finditer(line):
            value = float(match.group(1))
            unit = (match.group(2) or "px").lower()
            if value == 0:  # 例外 4
                continue
            if value * _UNIT_PX[unit] < _MIN_PX - 1e-9:
                yield f"{rel}:{lineno}: {match.group(0)}（约 {value * _UNIT_PX[unit]:.2f}px）"


class UiFontSizeTests(SimpleTestCase):
    def test_screen_text_is_at_least_12px(self):
        files = list(_targets())
        self.assertGreaterEqual(
            len(files), 100, "扫描文件数异常偏少，守卫可能已失效"
        )
        errors = []
        for rel, path in files:
            errors.extend(_violations(rel, path))
        self.assertEqual(errors, [], "屏幕 UI 出现 <12px 字号：\n" + "\n".join(errors))

    def test_font_size_exemptions_are_documented(self):
        """例外文件必须真实存在，避免有人改名后 allowlist 变成永久盲区。"""
        for rel in _ALLOWLISTED_CSS:
            self.assertTrue((ROOT / rel).is_file(), f"已登记的例外文件不存在：{rel}")
        marker = (
            ROOT / "knowledge_base/templates/knowledge_base/document_detail.html"
        ).read_text(encoding="utf-8")
        self.assertIn(_EXEMPT_MARK, marker, "SVG 字号例外必须就地写明 ui-font-exempt 及理由")
