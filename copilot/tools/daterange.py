"""从自然语句里抽"日期区间"。

为什么单独一个模块：日期口径是**最容易猜错、且猜错后果最严重**的一类信息。
把它从工具函数里拎出来，才能脱离 Django / 数据库单独写单测——这类纯函数
的回归成本极低，值得为它付这点结构代价。

支持的写法（都是真实问句里出现过的）：

- ``2024-03-01 到 2024-06-30``、``2024-03 ~ 2024-06``
- ``2024年3月到6月``（同年只给一次年份）
- ``2019到2021``（只给年份）
- ``2024年``、``2024年3月``（单年 / 单月）
- ``2024年三季度``、``2024年Q3``、``今年一季度``
- ``最近30天``、``过去3个月``、``近2年``（滚动窗口，按**当天**往前推）
- ``Jan 2024 to Mar 2024``（英文月份）

刻意不支持：``本月/上月/今年``。这些词各模块语义不同（是创建日期还是到期
日期？），本地链路猜了就可能是错的——计划书 §PLANNER_DATE 定的规矩：
带这类模糊时间词的问题一律不本地统计。
"""
from __future__ import annotations

import calendar
import datetime as _dt
import re

__all__ = ['extract_date_range', 'DateRange']

MIN_YEAR = 1970
_RANGE_SEP = r'(?:~|～|-|—|–|到|至|至到|until|to|through)'

_NUM = r'\d{1,4}'
_ISO = r'\d{4}-\d{1,2}(?:-\d{1,2})?'

# ① ISO 区间：2024-03 ~ 2024-06
_ISO_RANGE_RE = re.compile(
    rf'({_ISO})\s*{_RANGE_SEP}\s*({_ISO})', re.IGNORECASE)
# ② 中文年月日（年份可省）：2024年3月到6月
_CN_MONTH_RANGE_RE = re.compile(
    rf'(?:({_NUM})\s*年)?\s*({_NUM})\s*月\s*(?:({_NUM})\s*日)?\s*'
    rf'{_RANGE_SEP}\s*(?:({_NUM})\s*年)?\s*({_NUM})\s*月\s*(?:({_NUM})\s*日)?')
# ③ 中文带年份的完整区间：2024年3月5日到2024年6月8日
_CN_FULL_RANGE_RE = re.compile(
    rf'({_NUM})\s*年\s*(?:({_NUM})\s*月)?\s*(?:({_NUM})\s*日)?\s*'
    rf'{_RANGE_SEP}\s*({_NUM})\s*年\s*(?:({_NUM})\s*月)?\s*(?:({_NUM})\s*日)?')
# ④ 纯年份区间：2019到2021
_YEAR_RANGE_RE = re.compile(rf'(19\d{{2}}|20\d{{2}})\s*{_RANGE_SEP}\s*(19\d{{2}}|20\d{{2}})')
# ⑤ 单年：2024年
_SINGLE_YEAR_RE = re.compile(r'(19\d{2}|20\d{2})\s*年(?!\s*\d{1,2}\s*月)')
# ⑥ 单月：2024年3月
_SINGLE_MONTH_RE = re.compile(r'(19\d{2}|20\d{2})\s*年\s*(\d{1,2})\s*月(?!\s*\d{1,2}\s*日)')
# ⑦ 季度：2024年三季度 / 2024 Q3 / Q3 2024 / 今年一季度
#    年份既可能写在季度之前也可能在之后（"Q3 2024"），用命名分组，
#    免得改动时数错组号——这是最容易悄悄失效的一类正则。
_QUARTER_RE = re.compile(
    r'(?:(?P<y_cn>19\d{2}|20\d{2})\s*年|今年|本年)?\s*(?:第)?\s*'
    r'(?P<q_cn>[1-4一二三四])\s*季度'
    r'|(?:(?P<y_pre>19\d{2}|20\d{2})\s*)?[qQ]\s*(?P<q_en>[1-4])\b'
    r'(?:\s*(?P<y_post>19\d{2}|20\d{2}))?'
)
# ⑧ 滚动窗口：最近30天 / 过去3个月 / 近2年
_WINDOW_RE = re.compile(
    r'(最近|过去|近)\s*(\d{1,3})\s*(天|日|周|个月|月|年)')
# ⑨ 英文月份：Jan 2024 to Mar 2024
_EN_MONTH_RANGE_RE = re.compile(
    r'([A-Za-z]{3,9})\.?\s*(19\d{2}|20\d{2})\s*'
    rf'{_RANGE_SEP}\s*'
    r'([A-Za-z]{3,9})\.?\s*(19\d{2}|20\d{2})', re.IGNORECASE)

_EN_MONTHS = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'sept': 9, 'oct': 10, 'nov': 11, 'dec': 12,
}
_CN_DIGITS = {'一': 1, '二': 2, '三': 3, '四': 4}


class DateRange:
    """一个抽出来的区间；``label`` 用于回给人看的口径说明。"""

    __slots__ = ('start', 'end', 'label', 'source')

    def __init__(self, start, end, label, source):
        self.start = start
        self.end = end
        self.label = label
        self.source = source

    def as_dict(self):
        return {
            'start': self.start.isoformat() if self.start else None,
            'end': self.end.isoformat() if self.end else None,
            'label': self.label,
            'source': self.source,
        }

    def __repr__(self):  # pragma: no cover - 调试用
        return f'<DateRange {self.start}..{self.end} ({self.source})>'


def _month_end(year, month):
    return _dt.date(year, month, calendar.monthrange(year, month)[1])


def _valid_year(year, today):
    return MIN_YEAR <= year <= today.year + 1


def _parse_iso(value):
    parts = [int(p) for p in str(value).split('-')]
    year, month, day = parts[0], parts[1] if len(parts) > 1 else 1, \
        parts[2] if len(parts) > 2 else 1
    try:
        return _dt.date(year, month, day), len(parts)
    except ValueError:
        return None, 0


def _window_to_range(number, unit, today):
    """把"最近30天"换算成实际区间（含当天）。"""
    if unit in ('天', '日'):
        start = today - _dt.timedelta(days=number)
    elif unit == '周':
        start = today - _dt.timedelta(weeks=number)
    elif unit in ('个月', '月'):
        # 「最近 3 个月」= 当月 + 前 2 个完整自然月（BI 惯例）。
        # 若按"今天往前推 3 个自然月再取月初"算，7/1~10/31 实际是 4 个月的数据，
        # 用户看到的首值会比预期早一个月。
        month = today.month - (number - 1)
        year = today.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        start = _dt.date(year, month, 1)
    else:  # 年
        # 「近 2 年」同理：从两年前的同月 1 号起算（含当月）
        start = _dt.date(today.year - number, today.month, 1)
    return start, today


def extract_date_range(text, today=None):
    """抽日期区间；抽不到返回 ``None``。

    返回 :class:`DateRange`，其中 ``start``/``end`` 一定是合法 ``date``，
    且 ``start <= end``（输入倒着写时自动交换，用户写反不该被当成查询失败）。
    """
    if not text or not str(text).strip():
        return None
    today = today or _dt.date.today()
    src = str(text)

    # ⑨ 英文月份区间（放前面：它也含 ~ 到 之类的分隔符，先被别的规则吃掉会出错）
    match = _EN_MONTH_RANGE_RE.search(src)
    if match:
        first_month = _EN_MONTHS.get(match.group(1)[:4].lower())
        first_year = int(match.group(2))
        second_month = _EN_MONTHS.get(match.group(3)[:4].lower())
        second_year = int(match.group(4))
        if first_month and second_month and _valid_year(first_year, today) \
                and _valid_year(second_year, today):
            start = _dt.date(first_year, first_month, 1)
            end = _month_end(second_year, second_month)
            if start <= end:
                return DateRange(
                    start, end,
                    f'{start.isoformat()} ~ {end.isoformat()}', 'en_month_range')

    # ① ISO 区间
    match = _ISO_RANGE_RE.search(src)
    if match:
        left, left_parts = _parse_iso(match.group(1))
        right, right_parts = _parse_iso(match.group(2))
        if left and right:
            # 先交换再补月尾：倒着写（"2024-06 到 2024-03"）时如果先给右端
            # 补成 03-31 再交换，边界会变成 03-31 ~ 06-01，两头都错了。
            if left > right:
                left, right = right, left
            # 右端精度不足时补到该月的最后一天：查"3月到6月"的人不会想要只到 6/1
            if right_parts < 3:
                right = _month_end(right.year, right.month)
            return DateRange(left, right,
                             f'{left.isoformat()} ~ {right.isoformat()}', 'iso_range')

    # ③ 中文完整区间（两端都可能带年月日）
    match = _CN_FULL_RANGE_RE.search(src)
    if match:
        start_tuple = (match.group(1), match.group(2), match.group(3))
        end_tuple = (match.group(4), match.group(5), match.group(6))
        start = _cn_to_date(start_tuple, today, fallback_month=1, fallback_day=1)
        end = _cn_to_date(end_tuple, today, fallback_month=12, fallback_day=31,
                          end_of_month=True)
        if start and end:
            if start > end:
                start, end = end, start
            return DateRange(start, end,
                             f'{start.isoformat()} ~ {end.isoformat()}', 'cn_full_range')

    # ② 同年或跨年只写一次的写法：3月到6月 / 2024年3月到6月
    match = _CN_MONTH_RANGE_RE.search(src)
    if match:
        start_year = int(match.group(1) or today.year)
        start_month = int(match.group(2))
        end_year = int(match.group(4) or start_year)
        end_month = int(match.group(5))
        try:
            start = _dt.date(start_year, start_month, int(match.group(3) or 1))
            end_day = int(match.group(6) or 0) or calendar.monthrange(
                end_year, end_month)[1]
            end = _dt.date(end_year, end_month, end_day)
        except ValueError:
            start = end = None
        if start and end and start <= end:
            return DateRange(start, end,
                             f'{start.isoformat()} ~ {end.isoformat()}', 'cn_month_range')

    # ④ 纯年份区间
    match = _YEAR_RANGE_RE.search(src)
    if match:
        first, second = int(match.group(1)), int(match.group(2))
        if _valid_year(first, today) and _valid_year(second, today):
            start = _dt.date(min(first, second), 1, 1)
            end = _dt.date(max(first, second), 12, 31)
            return DateRange(start, end, f'{start.year} ~ {end.year} 全年',
                             'year_range')

    # ⑦ 季度
    match = _QUARTER_RE.search(src)
    if match:
        if match.group('q_cn'):
            quarter = _CN_DIGITS.get(match.group('q_cn')) or int(match.group('q_cn'))
        elif match.group('q_en'):
            quarter = int(match.group('q_en'))
        else:
            quarter = None
        year = int(match.group('y_cn') or match.group('y_pre')
                   or match.group('y_post') or today.year)
        if quarter and _valid_year(year, today):
            first_month = (quarter - 1) * 3 + 1
            start = _dt.date(year, first_month, 1)
            end = _month_end(year, first_month + 2)
            return DateRange(start, end, f'{year}年第{quarter}季度', 'quarter')

    # ⑧ 滚动窗口
    match = _WINDOW_RE.search(src)
    if match:
        number = int(match.group(2))
        start, end = _window_to_range(number, match.group(3), today)
        if start <= end:
            return DateRange(start, end,
                             f'{start.isoformat()} ~ {end.isoformat()}',
                             'rolling_window')

    # ⑥ 单月（要排在单年之前，否则"2024年3月"会被单年规则先吃掉）
    match = _SINGLE_MONTH_RE.search(src)
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        if _valid_year(year, today) and 1 <= month <= 12:
            start = _dt.date(year, month, 1)
            end = _month_end(year, month)
            return DateRange(start, end, f'{year}年{month}月', 'single_month')

    # ⑤ 单年
    match = _SINGLE_YEAR_RE.search(src)
    if match:
        year = int(match.group(1))
        if _valid_year(year, today):
            return DateRange(_dt.date(year, 1, 1), _dt.date(year, 12, 31),
                             f'{year} 年全年', 'single_year')

    return None


def _cn_to_date(groups, today, fallback_month=1, fallback_day=1,
                end_of_month=False):
    """把（年, 月, 日）三元组补成 date；缺的部分按"最宽口径"补齐。"""
    year_raw, month_raw, day_raw = groups
    year = int(year_raw) if year_raw else today.year
    if not _valid_year(year, today):
        return None
    month = int(month_raw) if month_raw else fallback_month
    if not 1 <= month <= 12:
        return None
    if day_raw:
        day = int(day_raw)
    elif end_of_month:
        day = calendar.monthrange(year, month)[1]
    else:
        day = fallback_day
    try:
        return _dt.date(year, month, day)
    except ValueError:
        return None
