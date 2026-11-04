"""业务单号生成：`前缀-YYYYMM-序号`，序号取当月已有最大值 + 1。

- 客户账期单 credit_no 用 `KQ-` 前缀，客户评审单 review_no 用 `CR-` 前缀，
  两类单据前缀分开，避免同一串号码既是账期单又是评审单（方案定稿修正 4）。
- 序号按「前缀-年月」查询，因此按月归零；内网低频场景应用层取号即可，
  credit_no 空值统一存 NULL（照客户台账 archive_no 先例），为唯一约束留路。
"""

from django.utils import timezone


def next_serial(model, field, prefix, width=3):
    """生成 `前缀-YYYYMM-序号`。width 为序号位数（默认 3 位，999 内够用）。"""
    head = f'{prefix}-{timezone.localdate():%Y%m}'
    max_no = 0
    for value in model.objects.filter(**{f'{field}__startswith': f'{head}-'}).values_list(field, flat=True):
        try:
            no = int(str(value).rsplit('-', 1)[1])
        except (ValueError, IndexError, TypeError):
            continue
        max_no = max(max_no, no)
    return f'{head}-{max_no + 1:0{width}d}'


def clean_blank(value):
    """空字符串转 None：CharField 空值统一落 NULL，多条空串不会撞唯一约束。"""
    return value or None
