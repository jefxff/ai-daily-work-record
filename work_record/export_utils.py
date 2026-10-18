"""导出响应的公共处理：文件名编码 + Excel 公式注入防护。

两件事都不复杂，但分散在 6 处各写一遍就会漏（审计 #27 / #30）：
- 中文文件名直接放进 Content-Disposition 会变乱码；引号与换行会造成响应头注入；
- openpyxl 把 '=' 开头的字符串按公式存储，接收方用 Excel 打开即执行。
"""

import re
from urllib.parse import quote

_UNSAFE = re.compile(r'[\r\n"\\]')


def content_disposition(filename: str, ascii_fallback: str = "export.xlsx", inline: bool = False):
    """构造 Content-Disposition。

    filename* 用 RFC 5987 percent-encoding 携带原始中文名；
    filename 给出纯 ASCII 兜底名，供极老的客户端使用；
    同时删除引号/反斜杠/换行，避免响应头注入。
    """
    clean = _UNSAFE.sub("", str(filename or "")).strip() or ascii_fallback
    disposition = "inline" if inline else "attachment"
    return (
        f'{disposition}; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{quote(clean)}"
    )


def sanitize_xlsx_formulas(workbook):
    """把以 '=' 开头的字符串单元格改写为普通文本（前缀单引号）。

    只在 data_type == 'f' 时处理：openpyxl 已经把 '=...' 判成公式，
    其余字符串不受影响。
    """
    for ws in workbook.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if cell.data_type == "f" and isinstance(cell.value, str):
                    cell.value = "'" + cell.value
    return workbook
