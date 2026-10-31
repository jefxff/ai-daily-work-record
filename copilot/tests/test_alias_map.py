"""别名 / 昵称映射单测。

覆盖 [S1] 的后半段：跨语种、OCR 抖动、昵称简称这些"归一化做不到"的场景，
必须靠显式别名表接通，并且**双向都要通**。
"""
from django.test import SimpleTestCase

from copilot.tools.alias_map import (alias_group, are_aliases, canonical_key,
                                     expand_aliases)
from copilot.tools.text_utils import to_slug


class AliasTests(SimpleTestCase):

    def test_cross_language_alias_is_bidirectional(self):
        self.assertTrue(are_aliases('Benjamin Franklin', '本杰明·富兰克林'))
        self.assertTrue(are_aliases('本杰明·富兰克林', 'Benjamin Franklin'))
        self.assertTrue(are_aliases('Benjamin Franklin', '富兰克林'))

    def test_institution_abbreviation(self):
        self.assertTrue(are_aliases('MIT', 'Massachusetts Institute of Technology'))
        self.assertTrue(are_aliases('IBM', 'International Business Machines'))
        self.assertFalse(are_aliases('MIT', 'IBM'))

    def test_ocr_jitter_is_bridged(self):
        self.assertTrue(are_aliases('Ahmook Kaan', 'Ähmöök Kaan'))
        self.assertTrue(are_aliases('X Æ A-12', 'X Æ A-Xii'))

    def test_nicknames_map_to_canonical_form(self):
        self.assertTrue(are_aliases('William', 'Bill'))
        self.assertTrue(are_aliases('Katherine', 'Kate'))
        self.assertTrue(are_aliases('Robert', 'Bob'))
        # 昵称分组彼此独立，不能串到一起去
        self.assertFalse(are_aliases('Bill', 'Bob'))

    def test_transitive_merge_through_shared_member(self):
        # William 同时是昵称表成员；本杰明/富兰克林组与昵称组不得互相污染
        self.assertTrue(are_aliases('William', 'Will'))
        self.assertFalse(are_aliases('Will', 'Benjamin Franklin'))

    def test_canonical_key_is_stable_across_surface_forms(self):
        first = canonical_key('MIT')
        second = canonical_key('Massachusetts Institute of Technology')
        third = canonical_key('mit')
        self.assertEqual(first, second)
        self.assertEqual(first, third)

    def test_canonical_key_of_unknown_value_is_its_own_slug(self):
        self.assertEqual(canonical_key('Some Unknown Person'),
                         to_slug('Some Unknown Person'))

    def test_alias_group_and_expand_aliases(self):
        group = alias_group('Will')
        self.assertIn(to_slug('William'), group)
        self.assertIn(to_slug('Bill'), group)
        self.assertEqual(sorted(group), expand_aliases('Will'))
        # 未知写法：组内只有它自己，接口保持一致
        self.assertEqual(expand_aliases('Nobody Here'), [to_slug('Nobody Here')])

    def test_empty_inputs_are_safe(self):
        self.assertFalse(are_aliases('', ''))
        self.assertFalse(are_aliases(None, 'William'))
        self.assertEqual(canonical_key(''), '')
        self.assertEqual(alias_group(None), frozenset())
        self.assertEqual(expand_aliases(''), [])
