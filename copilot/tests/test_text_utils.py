"""文本归一化单测。

覆盖 [S1] 的要求：同一个实体在不同来源里的写法（标点/空格/大小写/
全半角/重音/分隔号）必须归一到同一个 slug，否则 RecordLinkage 的
candidate blocking 会直接漏召回。
"""
import unicodedata

from django.test import SimpleTestCase

from copilot.tools.text_utils import (collapse_spaces, deentitize,
                                      strip_accents, to_slug)


class ToSlugTests(SimpleTestCase):

    def test_punctuation_and_case_and_spacing(self):
        self.assertEqual(to_slug('Test-Driven, Development.'),
                         'test-driven-development')
        self.assertEqual(to_slug('  Bleh   N/A  '), 'bleh-n-a')
        self.assertEqual(to_slug('BLEH Develop. Corp!'),
                         'bleh-develop-corp')

    def test_non_breaking_and_ideographic_space(self):
        self.assertEqual(to_slug('Benjamin Franklin'),
                         'benjamin-franklin')
        self.assertEqual(to_slug('Benjamin　Franklin'),
                         'benjamin-franklin')

    def test_accents_are_stripped_but_letters_are_not_replaced(self):
        # 重音（组合记号）会被去掉
        accented = 'Kath' + 'e' + unicodedata.lookup('COMBINING ACUTE ACCENT') + 'rine'
        self.assertEqual(to_slug(accented), 'katherine')
        # Æ / ø 是丹麦语里的**独立字母**，不是加在 e / o 上的重音，
        # NFKD 不会拆、这里也不会硬把它换成 ae / o ——那是跨文种别名折叠的活，
        # 属于 alias_map 的职责；归一化阶段不动它（见 [S1] 的边界说明）。
        self.assertEqual(to_slug('Ærø Andersen'), 'ærø-andersen')

    def test_separators_do_not_glue_tokens_together(self):
        # 分隔号必须变成分隔符，而不是被删掉后把两段粘成一个词
        self.assertEqual(to_slug('analytics:xe Go'), 'analytics-xe-go')
        self.assertEqual(to_slug('ウィリアム・ハリソン'),
                         'ウィリアム-ハリソン')

    def test_fullwidth_is_folded_to_halfwidth(self):
        self.assertEqual(to_slug('ＡＢＣ－１２３'), 'abc-123')

    def test_empty_and_symbol_only_inputs(self):
        self.assertEqual(to_slug(None), '')
        self.assertEqual(to_slug(''), '')
        self.assertEqual(to_slug('   '), '')
        self.assertEqual(to_slug('-----', fallback='na'), 'na')

    def test_ocr_variants_stay_distinct_after_normalization(self):
        # OCR 抖动：字符本身不同（字母 O vs 数字 0、罗马数字 Xii vs 阿拉伯 12），
        # 归一化解决不了，必须走 alias_map 显式登记别名
        self.assertNotEqual(to_slug('Ahm00k Kaan'), to_slug('Ahmook Kaan'))
        self.assertNotEqual(to_slug('X Æ A-12'), to_slug('X Æ A-Xii'))
        # 但"重音/变音字母"属于重音范畴，归一化应当把它俩拉平
        self.assertEqual(to_slug('Ähmöök Kaan'), to_slug('Ahmook Kaan'))

    def test_cross_script_keeps_distinct_slugs(self):
        # 同一作者的中文名与英文名不会自动等价——那是 alias_map 的活
        self.assertNotEqual(to_slug('富兰克林'), to_slug('Benjamin Franklin'))


class HelperTests(SimpleTestCase):

    def test_deentitize_common_entities(self):
        self.assertEqual(deentitize('R&amp;D&nbsp;Corp&#39;s'), "R&D Corp's")

    def test_strip_accents_keeps_other_scripts(self):
        self.assertEqual(strip_accents('成'), '成')
        self.assertEqual(strip_accents('Ж'),
                         unicodedata.normalize('NFKC', 'Ж'))

    def test_collapse_spaces(self):
        self.assertEqual(collapse_spaces('a　 \n\tb'), 'a b')
        self.assertEqual(collapse_spaces(None), '')
