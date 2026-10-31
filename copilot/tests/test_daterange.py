"""日期区间抽取单测。

时间口径一旦抽错，后面所有过滤都是白干，而且**错得很安静**（返回 0 条，
看起来像"没有数据"）。所以这里每种写法都钉一个用例。
"""
import datetime as dt

from django.test import SimpleTestCase

from copilot.tools.daterange import extract_date_range

TODAY = dt.date(2026, 10, 31)


class ExtractDateRangeTests(SimpleTestCase):

    def rng(self, text, today=TODAY):
        got = extract_date_range(text, today=today)
        self.assertIsNotNone(got, f'{text!r} 应当能抽出区间')
        return got.start, got.end, got.source

    def test_iso_day_precision(self):
        self.assertEqual(self.rng('2024-03-01 到 2024-06-30'),
                         (dt.date(2024, 3, 1), dt.date(2024, 6, 30), 'iso_range'))

    def test_iso_month_precision_end_extends_to_month_end(self):
        # 右端只写到月，应当补到该月最后一天——否则"2024-03 到 2024-06"会漏掉整月
        self.assertEqual(self.rng('2024-03 ~ 2024-06'),
                         (dt.date(2024, 3, 1), dt.date(2024, 6, 30), 'iso_range'))

    def test_chinese_same_year_month_range(self):
        self.assertEqual(self.rng('2024年3月到6月'),
                         (dt.date(2024, 3, 1), dt.date(2024, 6, 30), 'cn_month_range'))

    def test_chinese_full_range_with_days(self):
        self.assertEqual(self.rng('2024年3月5日至2024年6月8日'),
                         (dt.date(2024, 3, 5), dt.date(2024, 6, 8), 'cn_full_range'))

    def test_chinese_range_without_year(self):
        self.assertEqual(self.rng('3月到6月', today=TODAY),
                         (dt.date(2026, 3, 1), dt.date(2026, 6, 30), 'cn_month_range'))

    def test_year_only_range(self):
        self.assertEqual(self.rng('2019到2021'),
                         (dt.date(2019, 1, 1), dt.date(2021, 12, 31), 'year_range'))

    def test_single_year_and_single_month(self):
        self.assertEqual(self.rng('2024年')[:2],
                         (dt.date(2024, 1, 1), dt.date(2024, 12, 31)))
        self.assertEqual(self.rng('2024年3月')[:2],
                         (dt.date(2024, 3, 1), dt.date(2024, 3, 31)))

    def test_quarter_chinese_and_latin(self):
        self.assertEqual(self.rng('2024年三季度')[:2],
                         (dt.date(2024, 7, 1), dt.date(2024, 9, 30)))
        self.assertEqual(self.rng('2024 Q1')[:2],
                         (dt.date(2024, 1, 1), dt.date(2024, 3, 31)))
        self.assertEqual(self.rng('今年一季度', today=TODAY)[:2],
                         (dt.date(2026, 1, 1), dt.date(2026, 3, 31)))

    def test_rolling_windows(self):
        self.assertEqual(self.rng('最近30天', today=TODAY)[:2],
                         (dt.date(2026, 10, 1), TODAY))
        self.assertEqual(self.rng('过去3个月', today=TODAY)[:2],
                         (dt.date(2026, 8, 1), TODAY))
        self.assertEqual(self.rng('近2年', today=TODAY)[:2],
                         (dt.date(2024, 10, 1), TODAY))

    def test_english_month_range(self):
        self.assertEqual(self.rng('Jan 2024 to Mar 2024')[:2],
                         (dt.date(2024, 1, 1), dt.date(2024, 3, 31)))

    def test_reversed_input_is_swapped(self):
        # 用户写反了不该导致"查不到"，交换后照常返回
        self.assertEqual(self.rng('2024-06 到 2024-03')[:2],
                         (dt.date(2024, 3, 1), dt.date(2024, 6, 30)))

    def test_returns_none_when_no_explicit_range(self):
        # 本月/今年这类模糊词故意不支持：交给模型，不本地猜
        for text in ('', '   ', '本月有哪些客户', '你好', '这个保险多少钱',
                     '列出全部记录'):
            self.assertIsNone(extract_date_range(text, today=TODAY), text)

    def test_implausible_years_are_rejected(self):
        self.assertIsNone(extract_date_range('0999年3月', today=TODAY))
        self.assertIsNone(extract_date_range('3025年', today=TODAY))

    def test_as_dict_iso_format(self):
        got = extract_date_range('2024年', today=TODAY)
        self.assertEqual(got.as_dict()['start'], '2024-01-01')
        self.assertEqual(got.as_dict()['end'], '2024-12-31')
        self.assertEqual(got.as_dict()['label'], '2024 年全年')
