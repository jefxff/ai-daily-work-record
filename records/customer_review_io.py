"""客户评审 / 付款穿行测试的 Excel 导入、导出与模板。

三张评审表（信用评价 / 定期考评 / 现场审核）字段差异大，但都来自
``credit_spec`` 的同一套规则，因此这里不维护第二份字段清单：列定义由规则
模块现算（评分项、审核项、财务项都从 ``spec`` 取），改规则即改表头。

导出分两种形态，用途不同：

1. **列表导出**（``review_export_response``）：一行一份表单，按表单类型分
   sheet，评分项按列展开。列结构与导入模板完全一致，构成「导出 → 修改 →
   导入」闭环，矩阵式便于透视与汇总。
2. **单份导出**（``review_form_response``）：还原纸质表单的纵向版式（表头
   区 / 财务区 / 评分表 / 结论区），用于归档、发客户、打印，不参与导入。

导入语义（与客户台账导入保持一致）：

- 去重键：表单编号 →（客户全称 + 表单日期 + 表单类型）；
- 默认仅补空，可切换为覆盖；同一文件内去重键不得重复；
- 阻断错误整批不写入，确认阶段单事务全成全败；
- 合计得分、评定等级、审核结论、结论类列一律由服务端重算，不采信 Excel
  里的值 —— 否则改一个单元格就能造出「100 分 A 级」。
"""

import io
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone

from work_record.export_utils import content_disposition, sanitize_xlsx_formulas

from . import credit_spec as spec
from .models import CustomerReview, PaymentWalkthrough

MAX_IMPORT_ROWS = 2000
IMPORT_MAX_SIZE = 10 * 1024 * 1024

SHEET_BY_TYPE = {
    CustomerReview.TYPE_CREDIT: '信用评价',
    CustomerReview.TYPE_REVIEW: '定期考评',
    CustomerReview.TYPE_AUDIT: '现场审核',
}
TYPE_BY_SHEET = {v: k for k, v in SHEET_BY_TYPE.items()}
TYPE_BY_LABEL = {label: value for value, label in CustomerReview.REVIEW_TYPES}

NOTES_SHEET = '填写说明'


# ---------------------------------------------------------------------------
# 单元格解析（与客户台账导入同一套口径，保持两张表体验一致）
# ---------------------------------------------------------------------------
def _cell_text(value):
    """任意单元格值 → 去空白字符串；日期按 YYYY-MM-DD。"""
    if value is None:
        return ''
    if isinstance(value, datetime):
        return value.strftime('%Y-%m-%d')
    if isinstance(value, date):
        return value.strftime('%Y-%m-%d')
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _to_decimal(raw):
    """金额/数值原文 → Decimal；空值与非数值返回 None。"""
    text = _cell_text(raw).replace(',', '').replace('，', '')
    if not text or text in {'/', '-', '—', '无', '未披露', '不适用'}:
        return None
    text = text.replace('万元', '').replace('万', '').replace('元', '')
    try:
        return Decimal(text).quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError):
        return None


def _to_int(raw):
    value = _to_decimal(raw)
    return int(value) if value is not None else None


_NUM_RE = re.compile(r'-?\d+(?:\.\d+)?')


def _to_score(raw):
    """得分单元格 → int；'5'、'5分'、'5 分（≥5000 万）' 均可识别。"""
    text = _cell_text(raw)
    if not text:
        return None
    match = _NUM_RE.search(text)
    if not match:
        return None
    return int(float(match.group()))


def _to_date(raw):
    """日期单元格 → date；空值或无法识别返回 None。"""
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    text = _cell_text(raw).split(' ')[0].replace('年', '-').replace('月', '-').replace('日', '')
    if not text:
        return None
    for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%Y.%m.%d', '%Y%m%d'):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _display(value):
    """模型值 → 单元格值（Decimal 去尾零、日期转字符串）。"""
    if value is None:
        return ''
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.strftime('%Y-%m-%d')
    return value


# ---------------------------------------------------------------------------
# 文件解析
# ---------------------------------------------------------------------------
def _validate_file(f):
    from work_record.file_validation import content_matches, normalize_ext, read_head

    ext = normalize_ext(getattr(f, 'name', '') or '')
    if ext not in {'XLSX', 'XLS'}:
        raise ValueError(
            f'不支持的文件类型「{ext or "无扩展名"}」；仅支持 .xlsx / .xls'
        )
    if getattr(f, 'size', 0) > IMPORT_MAX_SIZE:
        raise ValueError(
            f'文件大小 {f.size / 1024 / 1024:.1f}MB 超过上限 '
            f'{IMPORT_MAX_SIZE // 1024 // 1024}MB'
        )
    if not content_matches(f, ext, read_head(f)):
        raise ValueError('文件内容与 Excel 扩展名不符，已拒绝解析')
    return ext


def _read_xlsx(f):
    from openpyxl import load_workbook

    wb = load_workbook(f, data_only=True, read_only=True)
    sheets = {}
    for name in wb.sheetnames:
        rows = list(wb[name].iter_rows(values_only=True))
        sheets[name] = _rows_to_dicts(rows)
    wb.close()
    return sheets


def _read_xls(f):
    try:
        import xlrd
    except ImportError:
        raise ValueError('服务器缺少 xlrd 依赖，暂时无法解析 .xls；请另存为 .xlsx 后重试')

    wb = xlrd.open_workbook(file_contents=f.read())
    sheets = {}
    for sheet in wb.sheets():
        rows = []
        for r in range(sheet.nrows):
            row = []
            for c in range(sheet.ncols):
                cell = sheet.cell(r, c)
                if cell.ctype == xlrd.XL_CELL_DATE:
                    try:
                        value = xlrd.xldate_as_datetime(cell.value, wb.datemode)
                    except (ValueError, OverflowError):
                        value = cell.value
                    row.append(_cell_text(value))
                else:
                    row.append(cell.value)
            rows.append(tuple(row))
        sheets[sheet.name] = _rows_to_dicts(rows)
    return sheets


def _rows_to_dicts(rows):
    """首行作为表头，其余行转为 [{表头: 文本}]；全空行跳过。"""
    if not rows:
        return []
    headers = [_cell_text(h) for h in rows[0]]
    data = []
    for raw in rows[1:]:
        if all(v is None or _cell_text(v) == '' for v in raw):
            continue
        row = {}
        for index, header in enumerate(headers):
            if not header:
                continue
            row[header] = _cell_text(raw[index]) if index < len(raw) else ''
        data.append(row)
    return data


def parse_upload(f):
    """解析上传的 Excel → {sheet 名: [{表头: 文本}]}；格式错误抛 ValueError。"""
    ext = _validate_file(f)
    try:
        f.seek(0)
    except (AttributeError, OSError):
        pass
    return _read_xls(f) if ext == 'XLS' else _read_xlsx(f)


# ---------------------------------------------------------------------------
# 列定义：全部由 credit_spec 现算
# ---------------------------------------------------------------------------
BASE_COLUMNS = [
    ('表单编号', 'review_no', 'text'),
    ('客户全称', 'customer_name', 'text'),
    ('客户编码', 'customer_code', 'text'),
    ('客户类型', 'customer_type', 'choice'),
    ('表单日期', 'review_date', 'date'),
    ('考评周期', 'period', 'text'),
    ('结算方式', 'settle_method', 'text'),
    ('合作区域', 'region', 'text'),
    ('原信用等级', 'prev_grade', 'text'),
    ('原授信额度（万元）', 'prev_limit', 'amount'),
    ('对应申请编号', 'apply_no', 'text'),
    ('审核地址', 'audit_address', 'text'),
    ('陪同人员', 'companion', 'text'),
    ('编制人', 'reviewer', 'text'),
    ('审批人', 'approver', 'text'),
    ('结论与整改要求', 'conclusion', 'text'),
    ('建议信用额度（万元）', 'credit_limit', 'amount'),
    ('建议信用期限（天）', 'credit_days', 'int'),
]

FIELD_LABELS = {key: header for header, key, _kind in BASE_COLUMNS}
FIELD_LABELS.update({f['key']: f'{f["label"]}（万元）' for f in spec.FIN_FIELDS})


def _groups_for(review_type):
    if review_type == CustomerReview.TYPE_CREDIT:
        return spec.CREDIT_GROUPS
    if review_type == CustomerReview.TYPE_REVIEW:
        return spec.REVIEW_GROUPS
    return None


def _short(name):
    """「一、基本情况」→「基本情况」，用于表头与矩阵列名。"""
    return re.sub(r'^[一二三四五六七八九十]+、', '', name)


def review_columns(review_type):
    """某类表单的列定义：[(表头, 键, 类型)]。

    类型含义：text/choice/amount/int/date 为模型字段；score 为评分项键；
    verdict/note 为现场审核判定与说明；ro 为只读计算列（导入时忽略）。
    """
    cols = [('表单类型', 'review_type', 'ro')] + list(BASE_COLUMNS)

    if review_type == CustomerReview.TYPE_CREDIT:
        cols += [(f'{f["label"]}（万元）', f['key'], 'amount') for f in spec.FIN_FIELDS]
        # 由基础数据算出的比率：导出时带出，导入时一律重算（ro = 只读）
        cols += [
            (f'{r["label"]}（{r["unit"]}）' if r['unit'] else r['label'], r['key'], 'ro')
            for r in spec.RATIOS
        ]

    groups = _groups_for(review_type)
    if groups:
        for gi, ii, group, item in spec.iter_items(groups):
            header = f'{_short(group["name"])}·{item["name"]}'
            cols.append((header, spec.item_key(gi, ii), 'score'))

    if review_type == CustomerReview.TYPE_AUDIT:
        for index, row in enumerate(spec.AUDIT_ROWS):
            cols.append((f'{_short(row["name"])}·判定', f'v:r{index}', 'verdict'))
            cols.append((f'{_short(row["name"])}·情况说明', f'n:r{index}', 'note'))
        cols.append(('审核结论', 'audit_conclusion', 'ro'))

    if review_type != CustomerReview.TYPE_AUDIT:
        cols.append(('合计得分', '_total', 'ro'))
        cols.append(('评定等级', '_grade', 'ro'))
    return cols


def _cell_value(obj, key, kind):
    """模型实例 → 单元格值（评分/审核明细来自 JSONField）。"""
    if kind == 'score':
        scores = obj.item_scores if isinstance(obj.item_scores, dict) else {}
        return scores.get(key, '')
    if kind == 'verdict':
        verdicts = obj.audit_verdicts if isinstance(obj.audit_verdicts, dict) else {}
        return verdicts.get(key[2:], '')
    if kind == 'note':
        notes = obj.audit_notes if isinstance(obj.audit_notes, dict) else {}
        return notes.get(key[2:], '')
    if key == '_total':
        return float(obj.total_score or 0)
    if key == '_grade':
        return obj.grade
    if key == 'review_type':
        return obj.get_review_type_display()
    if key == 'audit_conclusion':
        return obj.audit_conclusion
    return _display(getattr(obj, key, ''))


# ---------------------------------------------------------------------------
# 样式（列表导出 / 单份导出共用）
# ---------------------------------------------------------------------------
def _style_header(ws, columns, widths=None):
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    thin = Side(style='thin', color='BFBFBF')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    fill = PatternFill('solid', fgColor='EAF1F8')
    for col, (header, _key, _kind) in enumerate(columns, start=1):
        cell = ws.cell(row=1, column=col)
        cell.font = Font(bold=True, size=10)
        cell.fill = fill
        cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        cell.border = border
        ws.column_dimensions[cell.column_letter].width = 14
    # 文本类列放宽，金额/日期列收窄
    for col, (header, _key, kind) in enumerate(columns, start=1):
        width = {'text': 18, 'amount': 14, 'int': 12, 'date': 13,
                 'choice': 12, 'score': 11, 'verdict': 11, 'note': 24,
                 'ro': 12}.get(kind, 14)
        if kind == 'text' and ('结论' in header or '说明' in header):
            width = 32
        ws.column_dimensions[ws.cell(row=1, column=col).column_letter].width = width
    ws.freeze_panes = 'A2'
    ws.auto_filter.ref = ws.dimensions


def _write_sheet(ws, columns, rows, title=None):
    ws.append([c[0] for c in columns])
    for row in rows:
        ws.append(row)
    # 样式与自动筛选放在数据写完之后：auto_filter 的范围取 ws.dimensions，
    # 先设会把范围锁在表头行，用户点筛选看不到数据。
    _style_header(ws, columns)


def _notes_sheet(wb, lines, title='填写说明'):
    from openpyxl.styles import Alignment, Font

    ws = wb.create_sheet(title)
    ws.append([title])
    ws['A1'].font = Font(bold=True, size=14)
    for line in lines:
        ws.append([line])
    ws.column_dimensions['A'].width = 96
    for row in ws.iter_rows(min_row=2):
        row[0].alignment = Alignment(wrap_text=True, vertical='top')
    return ws


def _xlsx_response(wb, filename, ascii_fallback='export.xlsx'):
    sanitize_xlsx_formulas(wb)
    buf = io.BytesIO()
    wb.save(buf)
    response = HttpResponse(
        buf.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = content_disposition(filename, ascii_fallback)
    return response


# ---------------------------------------------------------------------------
# 客户评审：列表导出 / 模板
# ---------------------------------------------------------------------------
_REVIEW_NOTES = [
    '1. 仅支持 .xlsx / .xls；单次最多 {max_rows} 行。三张表各占一个工作表，按工作表名区分表单类型。',
    '2. 必填列：客户全称、表单日期。「表单编号」非空时优先按编号匹配已有记录。',
    '3. 合计得分、评定等级、审核结论由系统按评分与判定重算，Excel 中填的值不生效。',
    '4. 评分列填分值（如 5 或「5 分」）。若超出该项档位，系统按最接近的合法档位收敛。',
    '5. 现场审核的「判定」列只接受：符合 / 待整改 / 不符合 / 不适用。',
    '6. 财务列单位为万元，可填「5000」「5,000」「5000万」，系统自动换算。',
    '7. 日期支持 2026-01-31、2026/1/31、2026年1月31日、20260131。',
    '8. 默认仅补空，不覆盖系统已有值；导入向导中可切换为覆盖。',
    '9. 附件（财务报表、证照、现场照片、测试底稿）不在 Excel 中导入，请在记录详情页上传。',
]


def _notes_lines():
    return [line.format(max_rows=MAX_IMPORT_ROWS) for line in _REVIEW_NOTES]


def review_export_response(queryset, filename_prefix='客户评审'):
    """列表导出：按表单类型分 sheet，一行一份表单。"""
    from openpyxl import Workbook

    objs = list(queryset)
    wb = Workbook()
    wb.remove(wb.active)
    for review_type, sheet_name in SHEET_BY_TYPE.items():
        columns = review_columns(review_type)
        rows = [
            [_cell_value(obj, key, kind) for _header, key, kind in columns]
            for obj in objs if obj.review_type == review_type
        ]
        _write_sheet(wb.create_sheet(sheet_name), columns, rows)
    _notes_sheet(wb, _notes_lines())
    filename = f'{filename_prefix}-{timezone.localtime().strftime("%Y%m%d")}.xlsx'
    return _xlsx_response(wb, filename)


def review_template_response():
    """导入模板：三张空表 + 填写说明（不放示例数据，避免被误导入）。"""
    return review_export_response([], filename_prefix='客户评审导入模板')


# ---------------------------------------------------------------------------
# 客户评审：单份导出的纵向表单
# ---------------------------------------------------------------------------
def _form_sheet(ws, obj):
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    thin = Side(style='thin', color='B7C4D1')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    bold = Font(bold=True, size=10)
    head_fill = PatternFill('solid', fgColor='EAF1F8')
    title_font = Font(bold=True, size=15)

    def label_cell(row, col, text):
        cell = ws.cell(row=row, column=col, value=text)
        cell.font = bold
        cell.fill = head_fill
        cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        cell.border = border
        return cell

    def value_cell(row, col, value, span=1):
        cell = ws.cell(row=row, column=col, value=value)
        cell.alignment = Alignment(vertical='center', wrap_text=True)
        cell.border = border
        if span > 1:
            ws.merge_cells(start_row=row, start_column=col,
                           end_row=row, end_column=col + span - 1)
            for offset in range(1, span):
                ws.cell(row=row, column=col + offset).border = border
        return cell

    def section(row, text):
        cell = ws.cell(row=row, column=1, value=text)
        cell.font = Font(bold=True, size=11)
        cell.fill = PatternFill('solid', fgColor='DCE6F1')
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)
        for col in range(1, 7):
            ws.cell(row=row, column=col).border = border

    for col, width in zip('ABCDEF', (22, 26, 18, 26, 18, 26)):
        ws.column_dimensions[col].width = width

    row = 1
    ws.cell(row=row, column=1, value=obj.get_review_type_display())
    ws.cell(row=row, column=1).font = title_font
    ws.cell(row=row, column=1).alignment = Alignment(horizontal='center', vertical='center')
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)
    ws.row_dimensions[row].height = 30

    # —— 表头区：三对「标签｜值」一行 ——
    head_fields = [
        ('表单编号', obj.review_no or ''),
        ('客户全称', obj.customer_name),
        ('客户编码', obj.customer_code or ''),
        ('客户类型', obj.customer_type),
        ('表单日期', _display(obj.review_date)),
        ('考评周期', obj.period or ''),
        ('结算方式', obj.settle_method or ''),
        ('合作区域', obj.region or ''),
        ('原信用等级', obj.prev_grade or ''),
        ('原授信额度（万元）', _display(obj.prev_limit)),
        ('对应申请编号', obj.apply_no or ''),
        ('审核地址', obj.audit_address or ''),
        ('陪同人员', obj.companion or ''),
        ('编制人', obj.reviewer or ''),
    ]
    row += 1
    for start in range(0, len(head_fields), 3):
        chunk = head_fields[start:start + 3]
        for index, (label, value) in enumerate(chunk):
            label_cell(row, index * 2 + 1, label)
            value_cell(row, index * 2 + 2, value)
        row += 1

    if obj.needs_finance:
        row += 1
        section(row, '财务基础数据与指标（万元）')
        base = {f['key']: getattr(obj, f['key'], None) for f in spec.FIN_FIELDS}
        computed = spec.compute_ratios(base)
        row += 1
        fin = [(f['label'], _display(getattr(obj, f['key'], None))) for f in spec.FIN_FIELDS]
        for start in range(0, len(fin), 3):
            chunk = fin[start:start + 3]
            for index, (label, value) in enumerate(chunk):
                label_cell(row, index * 2 + 1, label)
                value_cell(row, index * 2 + 2, value)
            row += 1
        row += 1
        section(row, '财务指标（系统计算）')
        for start in range(0, len(spec.RATIOS), 3):
            chunk = spec.RATIOS[start:start + 3]
            row += 1
            for index, ratio in enumerate(chunk):
                value = getattr(obj, ratio['key'], None)
                if value is None:
                    value = computed.get(ratio['key'])
                label_cell(row, index * 2 + 1, ratio['label'])
                value_cell(row, index * 2 + 2,
                           spec.format_ratio(ratio['key'],
                                             float(value) if value is not None else None))

    row += 1
    if obj.review_type == CustomerReview.TYPE_AUDIT:
        section(row, '现场审核')
        row += 1
        for col, text in enumerate(('审核项目', '判定', '情况说明'), start=1):
            label_cell(row, col, text)
        ws.merge_cells(start_row=row, start_column=3, end_row=row, end_column=6)
        verdicts = obj.audit_verdicts if isinstance(obj.audit_verdicts, dict) else {}
        notes = obj.audit_notes if isinstance(obj.audit_notes, dict) else {}
        for index, audit_row in enumerate(spec.AUDIT_ROWS):
            row += 1
            value_cell(row, 1, audit_row['name'])
            value_cell(row, 2, verdicts.get(f'r{index}', ''))
            value_cell(row, 3, notes.get(f'r{index}', ''), span=4)
        row += 1
        label_cell(row, 1, '审核结论')
        value_cell(row, 2, obj.audit_conclusion, span=5)
    else:
        groups = _groups_for(obj.review_type)
        scores = obj.item_scores if isinstance(obj.item_scores, dict) else {}
        scores = {k: int(v) for k, v in scores.items()
                  if str(v).lstrip('-').isdigit()}
        section(row, '评分明细')
        row += 1
        for col, text in enumerate(('维度', '评分项', '标准分值', '实际得分', '评分依据'), start=1):
            label_cell(row, col, text)
        ws.merge_cells(start_row=row, start_column=5, end_row=row, end_column=6)
        for group in spec.score_items(groups, scores):
            first_row = row + 1
            for item in group['items']:
                row += 1
                value_cell(row, 1, '')
                value_cell(row, 2, item['name'])
                value_cell(row, 3, item['max'])
                value_cell(row, 4, item['value'] if item['scored'] else '')
                value_cell(row, 5, item['value_text'], span=2)
            ws.merge_cells(start_row=first_row, start_column=1,
                           end_row=first_row + len(group['items']) - 1, end_column=1)
            cell = ws.cell(row=first_row, column=1, value=f'{group["name"]}（{group["got"]}/{group["max"]}）')
            cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        row += 1
        label_cell(row, 1, '合计得分')
        value_cell(row, 2, f'{float(obj.total_score or 0):g} / 100')
        label_cell(row, 3, '评定等级')
        value_cell(row, 4, obj.grade, span=3)

    row += 1
    section(row, '结论与授信建议')
    row += 1
    label_cell(row, 1, '结论与整改要求')
    value_cell(row, 2, obj.conclusion, span=5)
    ws.row_dimensions[row].height = 48
    row += 1
    label_cell(row, 1, '建议信用额度（万元）')
    value_cell(row, 2, _display(obj.credit_limit))
    label_cell(row, 3, '建议信用期限（天）')
    value_cell(row, 4, obj.credit_days)
    label_cell(row, 5, '审批人')
    value_cell(row, 6, obj.approver or '')
    row += 1
    label_cell(row, 1, '编制人')
    value_cell(row, 2, obj.reviewer or '')
    label_cell(row, 3, '创建时间')
    value_cell(row, 4, obj.created_at.strftime('%Y-%m-%d %H:%M') if obj.created_at else '')
    ws.print_area = f'A1:F{row}'


def review_form_response(obj):
    """单份导出：还原纸质表单版式，用于归档 / 发客户 / 打印。"""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_BY_TYPE[obj.review_type]
    _form_sheet(ws, obj)
    ws.page_setup.orientation = 'portrait'
    ws.page_setup.fitToWidth = 1
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    safe_name = re.sub(r'[\\/:*?"<>|]', '', obj.customer_name)[:40]
    filename = f'{obj.get_review_type_display()}-{safe_name}-{obj.review_date:%Y%m%d}.xlsx'
    return _xlsx_response(wb, filename)


# ---------------------------------------------------------------------------
# 客户评审：导入
# ---------------------------------------------------------------------------
def split_import_sheets(sheets):
    """Excel → {表单类型: [行]}；未识别的工作表跳过（含「填写说明」）。"""
    out = {}
    for name, rows in sheets.items():
        review_type = TYPE_BY_SHEET.get(name.strip())
        if not rows:
            continue
        if review_type is None:
            # 工作表名被改过时，退回用「表单类型」列判断
            first = rows[0].get('表单类型', '').strip()
            review_type = TYPE_BY_LABEL.get(first)
        if review_type is None:
            continue
        out.setdefault(review_type, []).extend(rows)
    return out


def _row_to_record(row, review_type):
    """一行 → (values, scores, verdicts, notes, errors, warnings)。"""
    columns = review_columns(review_type)
    values, scores, verdicts, notes = {}, {}, {}, {}
    errors, warnings = [], []
    seq = row.get('_row', '')

    for header, key, kind in columns:
        if kind == 'ro':
            continue
        raw = row.get(header, '')
        if kind == 'score':
            value = _to_score(raw)
            if value is not None:
                scores[key] = value
            elif _cell_text(raw):
                warnings.append(f'{seq}「{header}」的得分「{raw}」不是数字，已忽略')
            continue
        if kind == 'verdict':
            text = _cell_text(raw)
            if not text:
                continue
            if text in spec.AUDIT_VERDICTS:
                verdicts[key[2:]] = text
            else:
                warnings.append(
                    f'{seq}「{header}」的判定「{text}」不在可选范围'
                    f'（{" / ".join(spec.AUDIT_VERDICTS)}）内，已忽略')
            continue
        if kind == 'note':
            text = _cell_text(raw)
            if text:
                notes[key[2:]] = text[:500]
            continue
        if kind == 'amount':
            if _cell_text(raw):
                value = _to_decimal(raw)
                if value is None:
                    warnings.append(f'{seq}「{header}」的「{raw}」不是数字，已留空')
                else:
                    values[key] = value
            continue
        if kind == 'int':
            if _cell_text(raw):
                value = _to_int(raw)
                if value is None:
                    warnings.append(f'{seq}「{header}」的「{raw}」不是整数，已留空')
                else:
                    values[key] = value
            continue
        if kind == 'date':
            text = _cell_text(raw)
            if text:
                value = _to_date(text)
                if value is None:
                    errors.append(f'{seq}「{header}」的日期「{text}」无法识别')
                else:
                    values[key] = value
            continue
        if kind == 'choice':
            text = _cell_text(raw)
            allowed = [v for v, _ in CustomerReview.CUSTOMER_TYPES]
            if not text:
                continue
            if text in allowed:
                values[key] = text
            else:
                warnings.append(f'{seq}「{header}」的「{text}」不在可选项内，已按「经销商」处理')
                values[key] = '经销商'
            continue
        values[key] = _cell_text(raw)[:2000] if key == 'conclusion' else _cell_text(raw)[:200]

    if not values.get('customer_name'):
        errors.append(f'{seq}客户全称为空')
    if not values.get('review_date'):
        if not any('表单日期' in e for e in errors):
            errors.append(f'{seq}表单日期为空')
    return values, scores, verdicts, notes, errors, warnings


def _match_review(values, review_type):
    """按 表单编号 →（客户全称 + 表单日期 + 类型）定位已有记录。"""
    review_no = values.get('review_no')
    if review_no:
        obj = CustomerReview.objects.filter(review_no=review_no).first()
        if obj:
            return obj, '表单编号'
    name = values.get('customer_name')
    day = values.get('review_date')
    if name and day:
        obj = CustomerReview.objects.filter(
            customer_name=name, review_date=day, review_type=review_type
        ).first()
        if obj:
            return obj, '客户全称+表单日期'
    return None, ''


def _review_changes(values, scores, obj, review_type, overwrite):
    """本行将写入的内容 [(标签, 值)]；obj 为 None 表示新建。"""
    changes = []
    for key, new in values.items():
        if new in ('', None):
            continue
        if obj is not None:
            old = getattr(obj, key, None)
            if not overwrite and old not in ('', None):
                continue
            if old == new:
                continue
        changes.append((FIELD_LABELS.get(key, key), _display(new)))

    if scores:
        old_scores = obj.item_scores if (obj and isinstance(obj.item_scores, dict)) else {}
        names = {key: header for header, key, kind in review_columns(review_type) if kind == 'score'}
        for key, value in scores.items():
            if obj is not None and not overwrite and key in old_scores:
                continue
            if old_scores.get(key) == value:
                continue
            changes.append((names.get(key, key), value))
    return changes


def build_review_preview(rows_by_type, overwrite=False):
    """构建导入预览：items / errors / warnings / summary。"""
    items, errors, warnings = [], [], []
    seen = {}
    total_rows = 0

    for review_type, rows in rows_by_type.items():
        for index, row in enumerate(rows, start=2):
            total_rows += 1
            row['_row'] = f'{SHEET_BY_TYPE[review_type]}第 {index} 行：'
            values, scores, verdicts, notes, row_errors, row_warnings = _row_to_record(
                row, review_type)
            errors.extend(row_errors)
            warnings.extend(row_warnings)
            if row_errors:
                continue

            dedupe_key = values.get('review_no') or (
                values.get('customer_name'), values.get('review_date'), review_type)
            if dedupe_key in seen:
                errors.append(
                    f'{row["_row"]}与第 {seen[dedupe_key]} 行重复'
                    f'（表单编号或客户全称+表单日期相同）')
                continue
            seen[dedupe_key] = index

            obj, matched_by = _match_review(values, review_type)
            changes = _review_changes(values, scores, obj, review_type, overwrite)
            if obj is None:
                action = 'create'
            elif changes:
                action = 'update'
            else:
                action = 'skip'

            items.append({
                'review_type': review_type,
                'type_label': dict(CustomerReview.REVIEW_TYPES)[review_type],
                'row': index,
                'row_label': row['_row'].rstrip('：'),
                'action': action,
                'customer_name': values.get('customer_name', ''),
                # 预览表两种表共用一列，统一用 display_name 供模板渲染
                'display_name': values.get('customer_name', ''),
                'review_no': values.get('review_no', ''),
                'matched_by': matched_by,
                'matched_id': getattr(obj, 'pk', None),
                'matched_name': getattr(obj, 'customer_name', ''),
                'changes': changes,
                'values': values,
                'scores': scores,
                'verdicts': verdicts,
                'notes': notes,
            })

    summary = {
        'create': sum(1 for i in items if i['action'] == 'create'),
        'update': sum(1 for i in items if i['action'] == 'update'),
        'skip': sum(1 for i in items if i['action'] == 'skip'),
        'error': len(errors),
        'total': total_rows,
    }
    return {'items': items, 'errors': errors, 'warnings': warnings, 'summary': summary}


def apply_review_rows(rows_by_type, overwrite=False, user=None):
    """按预览写库：阻断错误先全拦，写入单事务全成全败。"""
    preview = build_review_preview(rows_by_type, overwrite=overwrite)
    if preview['errors']:
        raise ValueError('导入数据校验未通过：' + '；'.join(preview['errors'][:10]))

    created = updated = skipped = 0
    with transaction.atomic():
        for item in preview['items']:
            review_type = item['review_type']
            groups = _groups_for(review_type)
            values = dict(item['values'])

            if item['action'] == 'create':
                obj = CustomerReview(review_type=review_type, created_by=user)
                for key, value in values.items():
                    setattr(obj, key, value)
                scores = item['scores']
                obj.item_scores = scores
                if groups:
                    obj.total_score = spec.total_score(scores, groups)
                    obj.grade = spec.grade_of(float(obj.total_score),
                                              _grades(review_type))['label']
                else:
                    obj.audit_verdicts = item['verdicts']
                    obj.audit_notes = item['notes']
                    obj.audit_conclusion, _reason = spec.audit_conclusion(item['verdicts'])
                    obj.total_score = 0
                _fill_ratios(obj, review_type)
                obj.save()
                created += 1
                continue

            obj = CustomerReview.objects.select_for_update().get(pk=item['matched_id'])
            if item['action'] == 'skip':
                skipped += 1
                continue

            for key, value in values.items():
                if value in ('', None):
                    continue
                old = getattr(obj, key, None)
                if not overwrite and old not in ('', None):
                    continue
                setattr(obj, key, value)

            if groups:
                merged = dict(obj.item_scores) if isinstance(obj.item_scores, dict) else {}
                for key, value in item['scores'].items():
                    if not overwrite and key in merged:
                        continue
                    merged[key] = value
                obj.item_scores = merged
                obj.total_score = spec.total_score(merged, groups)
                obj.grade = spec.grade_of(float(obj.total_score), _grades(review_type))['label']
            else:
                if overwrite:
                    obj.audit_verdicts = item['verdicts']
                    obj.audit_notes = item['notes']
                else:
                    obj.audit_verdicts = {**(obj.audit_verdicts or {}), **item['verdicts']}
                    obj.audit_notes = {**(obj.audit_notes or {}), **item['notes']}
                obj.audit_conclusion, _reason = spec.audit_conclusion(obj.audit_verdicts or {})
            _fill_ratios(obj, review_type)
            obj.save()
            updated += 1

    return {
        'created': created, 'updated': updated, 'skipped': skipped,
        'errors': preview['errors'], 'warnings': preview['warnings'],
    }


def _grades(review_type):
    return spec.CREDIT_GRADES if review_type == CustomerReview.TYPE_CREDIT else spec.REVIEW_GRADES


def _fill_ratios(obj, review_type):
    """按基础数据重算比率并落库（只有信用评价表有财务区）。"""
    if review_type != CustomerReview.TYPE_CREDIT:
        return
    base = {f['key']: getattr(obj, f['key'], None) for f in spec.FIN_FIELDS}
    for key, value in spec.compute_ratios(base).items():
        setattr(obj, key, None if value is None else
                Decimal(str(value)).quantize(Decimal('0.01')))


# ---------------------------------------------------------------------------
# 付款穿行测试：列定义
# ---------------------------------------------------------------------------
WT_MAIN_COLUMNS = [
    ('测试编号', 'test_no', 'text'),
    ('被测试对象', 'payee_name', 'text'),
    ('对象类型', 'payee_type', 'choice'),
    ('测试期间起', 'period_start', 'date'),
    ('测试期间止', 'period_end', 'date'),
    ('测试执行日期', 'test_date', 'date'),
    ('抽样方法', 'sample_method', 'text'),
    ('总体笔数', 'population_size', 'int'),
    ('抽样笔数', 'sample_size', 'int'),
    ('账期约定', 'terms_text', 'text'),
    ('约定账期天数', 'terms_days', 'int'),
    ('账期起算口径', 'terms_base', 'text'),
    ('测试人', 'tester', 'text'),
    ('复核人', 'reviewer', 'text'),
    ('测试结论', 'conclusion', 'ro'),
    ('账期按期率(%)', 'on_time_rate', 'ro'),
    ('逾期笔数', 'overdue_count', 'ro'),
    ('缺陷汇总', 'defect_summary', 'text'),
    ('整改建议', 'suggestion', 'text'),
]

WT_SAMPLE_COLUMNS = [
    ('测试编号', 'test_no', 'text'),
    ('样本序号', 'seq', 'int'),
    ('凭证号', 'voucher_no', 'text'),
    ('金额', 'amount', 'text'),
    ('收款方', 'payee', 'text'),
    # 账期执行核对：约定账期留空则继承测试主表的天数
    ('约定账期天数', 'terms_days', 'int'),
    ('账期起算日', 'base_date', 'text'),
    ('到期日', 'due_date', 'text'),
    ('实际付款日期', 'pay_date', 'text'),
    ('偏差天数', 'delay_days', 'ro'),
    ('账期执行', 'terms_status', 'ro'),
    ('控制点编号', 'step_key', 'text'),
    ('控制点名称', 'step_name', 'text'),
    ('执行结果', 'result', 'text'),
    ('缺陷类型', 'defect', 'text'),
    ('单据编号', 'doc_no', 'text'),
    ('单据日期', 'doc_date', 'text'),
    ('说明', 'note', 'text'),
]

WT_FIELD_LABELS = {key: header for header, key, _kind in WT_MAIN_COLUMNS}


def _style_wt_headers(ws, columns):
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    thin = Side(style='thin', color='BFBFBF')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    fill = PatternFill('solid', fgColor='EAF1F8')
    for col, (header, _key, _kind) in enumerate(columns, start=1):
        cell = ws.cell(row=1, column=col)
        cell.font = Font(bold=True, size=10)
        cell.fill = fill
        cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        cell.border = border
        ws.column_dimensions[cell.column_letter].width = 18 if _kind == 'text' else 12
    ws.freeze_panes = 'A2'
    ws.auto_filter.ref = ws.dimensions


def _wt_sample_values(obj, sample, index, step_key, step_name, cell, terms):
    """样本明细一行：一笔样本 × 一个控制点。

    账期列逐行重复（而不是只在首行出现），是为了让导出的表格可以直接做
    数据透视：按控制点看逾期分布时不需要再补空值。
    偏差天数与账期执行由规则重算，属只读列。
    """
    delay = terms['delay_days']
    terms_days = sample.get('terms_days')
    return [
        obj.test_no, sample.get('seq') or index,
        sample.get('voucher_no', ''), sample.get('amount', ''),
        sample.get('payee', ''),
        '' if terms_days in (None, '') else terms_days,
        _display(terms['base_date']), _display(terms['due_date']),
        _display(terms['pay_date']),
        '' if delay is None else delay,
        terms['status'],
        step_key, step_name,
        cell.get('result', ''), cell.get('defect', '') or '无',
        cell.get('doc_no', ''), cell.get('doc_date', ''), cell.get('note', ''),
    ]


def walkthrough_export_response(queryset, filename_prefix='付款穿行测试'):
    """列表导出：主表一行一个测试，样本明细另置一个工作表。"""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = '测试清单'
    ws.append([c[0] for c in WT_MAIN_COLUMNS])

    sample_ws = wb.create_sheet('样本明细')
    sample_ws.append([c[0] for c in WT_SAMPLE_COLUMNS])

    step_names = {s['key']: s['name'] for s in spec.WALKTHROUGH_STEPS}
    for obj in queryset:
        samples = obj.samples if isinstance(obj.samples, list) else []
        terms_sum = spec.terms_stats(samples, obj.terms_days)
        ws.append([
            obj.test_no, obj.payee_name, obj.payee_type,
            _display(obj.period_start), _display(obj.period_end), _display(obj.test_date),
            obj.sample_method, obj.population_size, obj.sample_size,
            obj.terms_text, obj.terms_days, obj.terms_base,
            obj.tester, obj.reviewer, obj.conclusion,
            terms_sum['on_time_rate'] if terms_sum['on_time_rate'] is not None else '',
            terms_sum['overdue'],
            obj.defect_summary, obj.suggestion,
        ])
        for index, sample in enumerate(samples, start=1):
            steps = sample.get('steps') if isinstance(sample.get('steps'), dict) else {}
            terms = spec.sample_terms(sample, obj.terms_days)
            for step in spec.WALKTHROUGH_STEPS:
                cell = steps.get(step['key']) or {}
                sample_ws.append(_wt_sample_values(
                    obj, sample, index, step['key'], step_names.get(step['key'], ''),
                    cell, terms,
                ))

    _style_wt_headers(ws, WT_MAIN_COLUMNS)
    _style_wt_headers(sample_ws, WT_SAMPLE_COLUMNS)

    _notes_sheet(wb, [
        '1. 「测试清单」一行一个测试；「样本明细」一行 = 一个样本 × 一个控制点。',
        '2. 导入时两张表按「测试编号」关联；样本明细可留空，只导入测试主表。',
        '3. 执行结果只接受：执行 / 部分执行 / 未执行 / 不适用。',
        '4. 缺陷类型只接受：无 / 设计缺陷 / 执行缺陷。',
        '5. 测试结论由系统按样本与控制点判定重算，Excel 中填的值不生效。',
        '6. 「偏差天数」「账期执行」为只读列，由到期日与实际付款日期重算。',
        '7. 账期核对：到期日 = 起算日 + 约定账期天数（样本未填天数时继承主表）；'
        f'到期后 {spec.TERMS_GRACE_DAYS} 天内付款仍计为按期。',
        '8. 日期支持 2026-01-31、2026/1/31、20260131。',
    ])
    filename = f'{filename_prefix}-{timezone.localtime().strftime("%Y%m%d")}.xlsx'
    return _xlsx_response(wb, filename)


def walkthrough_template_response():
    return walkthrough_export_response([], filename_prefix='付款穿行测试导入模板')


def walkthrough_form_response(obj):
    """单份导出：测试主表 + 各控制点执行情况 + 整改建议。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    thin = Side(style='thin', color='B7C4D1')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    bold = Font(bold=True, size=10)
    head_fill = PatternFill('solid', fgColor='EAF1F8')

    wb = Workbook()
    ws = wb.active
    ws.title = '穿行测试'
    for col, width in zip('ABCDEFGH', (20, 22, 14, 14, 14, 14, 14, 14)):
        ws.column_dimensions[col].width = width

    def put(row, col, value, span=1, is_head=False):
        cell = ws.cell(row=row, column=col, value=value)
        cell.border = border
        cell.alignment = Alignment(vertical='center', wrap_text=True,
                                   horizontal='center' if is_head else 'left')
        if is_head:
            cell.font = bold
            cell.fill = head_fill
        if span > 1:
            ws.merge_cells(start_row=row, start_column=col, end_row=row, end_column=col + span - 1)
            for offset in range(1, span):
                ws.cell(row=row, column=col + offset).border = border
        return cell

    ws.cell(row=1, column=1, value='客户 / 供应商付款穿行测试')
    ws.cell(row=1, column=1).font = Font(bold=True, size=15)
    ws.cell(row=1, column=1).alignment = Alignment(horizontal='center', vertical='center')
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=8)
    ws.row_dimensions[1].height = 30

    head = [
        ('测试编号', obj.test_no or ''), ('被测试对象', obj.payee_name),
        ('对象类型', obj.payee_type), ('测试执行日期', _display(obj.test_date)),
        ('测试期间', f'{obj.period_start:%Y-%m-%d} 至 {obj.period_end:%Y-%m-%d}'),
        ('抽样方法', obj.sample_method or ''),
        ('总体笔数', obj.population_size), ('抽样笔数', obj.sample_size),
        ('账期约定', obj.terms_text or ''), ('约定账期天数', obj.terms_days or ''),
        ('账期起算口径', obj.terms_base or ''),
        ('测试人', obj.tester or ''), ('复核人', obj.reviewer or ''),
    ]
    row = 2
    for start in range(0, len(head), 3):
        for index, (label, value) in enumerate(head[start:start + 3]):
            put(row, index * 2 + 1, label, is_head=True)
            put(row, index * 2 + 2, _display(value))
        row += 1

    row += 1
    for col, text in enumerate(('控制点', '所属阶段', '执行数', '部分执行', '未执行',
                                '不适用', '执行率', '缺陷数'), start=1):
        put(row, col, text, is_head=True)
    samples = obj.samples if isinstance(obj.samples, list) else []
    stats = spec.walkthrough_stats(samples, obj.terms_days)
    for step in stats['steps']:
        row += 1
        put(row, 1, step['name'])
        put(row, 2, step['stage'])
        put(row, 3, step['executed'])
        put(row, 4, step['partial'])
        put(row, 5, step['not_executed'])
        put(row, 6, step['na'])
        put(row, 7, f'{step["rate"]}%')
        put(row, 8, step['defects'])

    row += 1
    put(row, 1, '测试结论', is_head=True)
    put(row, 2, obj.conclusion or '')
    put(row, 3, '缺陷合计', is_head=True)
    put(row, 4, stats['total_defects'])
    put(row, 5, '样本数', is_head=True)
    put(row, 6, stats['sample_count'])

    # 账期执行核对：这一块回答「钱是不是按约定期限付的」，与上面的控制点判定分开
    tstat = stats['terms']
    row += 1
    put(row, 1, '账期核对笔数', is_head=True)
    put(row, 2, tstat['checked'])
    put(row, 3, '账期按期率', is_head=True)
    put(row, 4, f"{tstat['on_time_rate']}%" if tstat['on_time_rate'] is not None else '—')
    put(row, 5, '逾期笔数', is_head=True)
    put(row, 6, tstat['overdue'])
    row += 1
    put(row, 1, '平均偏差(天)', is_head=True)
    put(row, 2, '' if tstat['avg_delay'] is None else tstat['avg_delay'])
    put(row, 3, '最长逾期(天)', is_head=True)
    put(row, 4, '' if tstat['max_delay'] is None else tstat['max_delay'])
    put(row, 5, '提前付款', is_head=True)
    put(row, 6, tstat['early'])

    row += 1
    put(row, 1, '缺陷汇总', is_head=True)
    put(row, 2, obj.defect_summary, span=7)
    ws.row_dimensions[row].height = 48
    row += 1
    put(row, 1, '整改建议', is_head=True)
    put(row, 2, obj.suggestion, span=7)
    ws.row_dimensions[row].height = 48

    samples = obj.samples if isinstance(obj.samples, list) else []
    if samples:
        ws2 = wb.create_sheet('样本明细')
        ws2.append([c[0] for c in WT_SAMPLE_COLUMNS])
        step_names = {s['key']: s['name'] for s in spec.WALKTHROUGH_STEPS}
        for index, sample in enumerate(samples, start=1):
            steps = sample.get('steps') if isinstance(sample.get('steps'), dict) else {}
            terms = spec.sample_terms(sample, obj.terms_days)
            for step in spec.WALKTHROUGH_STEPS:
                cell = steps.get(step['key']) or {}
                ws2.append(_wt_sample_values(
                    obj, sample, index, step['key'], step_names.get(step['key'], ''),
                    cell, terms,
                ))
        _style_wt_headers(ws2, WT_SAMPLE_COLUMNS)

    safe_name = re.sub(r'[\\/:*?"<>|]', '', obj.payee_name)[:40]
    filename = f'付款穿行测试-{safe_name}-{obj.test_date:%Y%m%d}.xlsx'
    return _xlsx_response(wb, filename)


# ---------------------------------------------------------------------------
# 付款穿行测试：导入
# ---------------------------------------------------------------------------
def _pick_sheet(sheets, *keywords):
    """按关键字取工作表（容忍用户改过表名）。"""
    for name, rows in sheets.items():
        for keyword in keywords:
            if keyword in name:
                return rows
    return []


def _row_to_walkthrough(row, seq_label):
    values, errors, warnings = {}, [], []
    for header, key, kind in WT_MAIN_COLUMNS:
        if kind == 'ro':
            continue
        raw = _cell_text(row.get(header, ''))
        if not raw:
            continue
        if kind == 'date':
            value = _to_date(raw)
            if value is None:
                errors.append(f'{seq_label}「{header}」的日期「{raw}」无法识别')
            else:
                values[key] = value
        elif kind == 'int':
            value = _to_int(raw)
            if value is None:
                warnings.append(f'{seq_label}「{header}」的「{raw}」不是整数，已留空')
            else:
                values[key] = value
        elif kind == 'choice':
            if raw in spec.PAYEE_TYPES:
                values[key] = raw
            else:
                warnings.append(f'{seq_label}「{header}」的「{raw}」不在可选项内，已按「供应商」处理')
                values[key] = '供应商'
        else:
            values[key] = raw[:2000] if key in ('defect_summary', 'suggestion') else raw[:100]

    if not values.get('payee_name'):
        errors.append(f'{seq_label}被测试对象为空')
    for key, label in (('period_start', '测试期间起'), ('period_end', '测试期间止'),
                       ('test_date', '测试执行日期')):
        if not values.get(key) and not any(label in e for e in errors):
            errors.append(f'{seq_label}{label}为空')
    if values.get('period_start') and values.get('period_end') \
            and values['period_start'] > values['period_end']:
        errors.append(f'{seq_label}测试期间起晚于测试期间止')
    return values, errors, warnings


def _samples_from_rows(sample_rows):
    """样本明细行 → {测试编号: [样本]}；非法控制点/结果直接跳过并记提示。"""
    allowed_steps = {s['key']: s for s in spec.WALKTHROUGH_STEPS}
    allowed_results = {r[0] for r in spec.STEP_RESULTS}
    grouped, warnings = {}, []
    for index, row in enumerate(sample_rows, start=2):
        test_no = _cell_text(row.get('测试编号', ''))
        step_key = _cell_text(row.get('控制点编号', ''))
        if not test_no:
            warnings.append(f'样本明细第 {index} 行：测试编号为空，已跳过')
            continue
        if step_key and step_key not in allowed_steps:
            # 允许填写控制点名称
            by_name = {s['name']: s['key'] for s in spec.WALKTHROUGH_STEPS}
            step_key = by_name.get(_cell_text(row.get('控制点名称', '')), '')
        if step_key and step_key not in allowed_steps:
            warnings.append(f'样本明细第 {index} 行：控制点「{step_key}」无法识别，已跳过')
            continue
        # 控制点列留空的行按「样本主信息行」处理：只登记单号、金额、账期，
        # 用于只想核对账期、不逐控制点判定的场景。这类行不产生控制点判定。

        result = _cell_text(row.get('执行结果', ''))
        if result and result not in allowed_results:
            warnings.append(f'样本明细第 {index} 行：执行结果「{result}」不在可选范围，已留空')
            result = ''
        defect = _cell_text(row.get('缺陷类型', '')) or '无'
        if defect not in spec.DEFECT_TYPES:
            warnings.append(f'样本明细第 {index} 行：缺陷类型「{defect}」不在可选范围，已按「无」处理')
            defect = '无'

        seq = _to_int(row.get('样本序号')) or 1
        # 账期字段：日期只接受可解析的写法，无法识别就留空（不阻断导入，
        # 因为账期核对是附加层，缺日期只影响「未核对」，不影响控制点判定）
        terms_days = None
        raw_days = _cell_text(row.get('约定账期天数', ''))
        if raw_days:
            terms_days = _to_int(raw_days)
            if terms_days is None or terms_days < 0:
                warnings.append(f'样本明细第 {index} 行：约定账期「{raw_days}」不是有效天数，已按继承主表处理')
                terms_days = None
        dates = {}
        for header, key in (('账期起算日', 'base_date'), ('到期日', 'due_date'),
                            ('实际付款日期', 'pay_date')):
            raw = _cell_text(row.get(header, ''))
            if not raw:
                dates[key] = ''
                continue
            parsed = _to_date(raw)
            if parsed is None:
                warnings.append(f'样本明细第 {index} 行：「{header}」的「{raw}」无法识别，已留空')
                dates[key] = ''
            else:
                dates[key] = parsed.isoformat()

        bucket = grouped.setdefault(test_no, {})
        sample = bucket.setdefault(seq, {
            'seq': seq,
            'voucher_no': _cell_text(row.get('凭证号', ''))[:100],
            'pay_date': '',
            'amount': _cell_text(row.get('金额', ''))[:50],
            'payee': _cell_text(row.get('收款方', ''))[:100],
            'terms_days': terms_days,
            'base_date': '',
            'due_date': '',
            'steps': {},
        })
        # 账期三列在 8 个控制点行里是重复的，任一非空即覆盖，取最后一次填的值
        if terms_days is not None:
            sample['terms_days'] = terms_days
        for key, value in dates.items():
            if value:
                sample[key] = value
        if step_key:
            sample['steps'][step_key] = {
                'result': result, 'defect': defect,
                'doc_no': _cell_text(row.get('单据编号', ''))[:100],
                'doc_date': _cell_text(row.get('单据日期', ''))[:20],
                'note': _cell_text(row.get('说明', ''))[:500],
            }
    return {k: [v[key] for key in sorted(v)] for k, v in grouped.items()}, warnings


def build_walkthrough_import_preview(sheets, overwrite=False):
    """穿行测试导入预览：主表 + 可选样本明细。"""
    main_rows = _pick_sheet(sheets, '测试清单', '穿行测试')
    sample_rows = _pick_sheet(sheets, '样本明细', '样本')

    samples_by_test, warnings = _samples_from_rows(sample_rows)
    items, errors = [], []
    warnings = list(warnings)
    seen = {}

    for index, row in enumerate(main_rows, start=2):
        label = f'测试清单第 {index} 行：'
        values, row_errors, row_warnings = _row_to_walkthrough(row, label)
        errors.extend(row_errors)
        warnings.extend(row_warnings)
        if row_errors:
            continue

        test_no = values.get('test_no')
        dedupe = test_no or (values.get('payee_name'), values.get('test_date'))
        if dedupe in seen:
            errors.append(f'{label}与第 {seen[dedupe]} 行重复（测试编号或对象+日期相同）')
            continue
        seen[dedupe] = index

        obj = None
        matched_by = ''
        if test_no:
            obj = PaymentWalkthrough.objects.filter(test_no=test_no).first()
            matched_by = '测试编号' if obj else ''
        if obj is None and values.get('payee_name') and values.get('test_date'):
            obj = PaymentWalkthrough.objects.filter(
                payee_name=values['payee_name'], test_date=values['test_date']
            ).first()
            matched_by = '对象+测试日期' if obj else ''

        samples = samples_by_test.get(test_no, []) if test_no else []
        old_samples = obj.samples if (obj and isinstance(obj.samples, list)) else []
        # 表头账期：本次导入填了就用导入值，否则沿用已有记录的天数
        default_terms = values.get('terms_days')
        if default_terms is None and obj is not None:
            default_terms = obj.terms_days
        if samples:
            stats = spec.walkthrough_stats(samples, default_terms)
            conclusion = stats['conclusion'] or '无效'
        else:
            stats = spec.walkthrough_stats(old_samples, default_terms)
            conclusion = stats['conclusion'] or (_cell_text(row.get('测试结论', '')) or '无效')

        changes = []
        for key, new in values.items():
            if new in ('', None):
                continue
            old = getattr(obj, key, None) if obj else None
            if obj is not None and not overwrite and old not in ('', None):
                continue
            if old != new:
                changes.append((WT_FIELD_LABELS.get(key, key), _display(new)))
        if obj is None:
            action = 'create'
        elif changes or samples:
            action = 'update'
        else:
            action = 'skip'

        items.append({
            'row': index, 'row_label': label.rstrip('：'), 'action': action,
            'payee_name': values.get('payee_name', ''),
            'display_name': values.get('payee_name', ''),
            'test_no': test_no or '',
            'matched_by': matched_by, 'matched_id': getattr(obj, 'pk', None),
            'matched_name': getattr(obj, 'payee_name', ''),
            'changes': changes, 'values': values, 'samples': samples,
            'conclusion': conclusion, 'sample_count': len(samples),
            'overdue': stats['terms']['overdue'],
            'on_time_rate': stats['terms']['on_time_rate'],
        })

    summary = {
        'create': sum(1 for i in items if i['action'] == 'create'),
        'update': sum(1 for i in items if i['action'] == 'update'),
        'skip': sum(1 for i in items if i['action'] == 'skip'),
        'error': len(errors),
        'total': len(main_rows),
    }
    return {'items': items, 'errors': errors, 'warnings': warnings, 'summary': summary}


def apply_walkthrough_import(sheets, overwrite=False, user=None):
    preview = build_walkthrough_import_preview(sheets, overwrite=overwrite)
    if preview['errors']:
        raise ValueError('导入数据校验未通过：' + '；'.join(preview['errors'][:10]))

    created = updated = skipped = 0
    with transaction.atomic():
        for item in preview['items']:
            values = item['values']
            if item['action'] == 'create':
                obj = PaymentWalkthrough(created_by=user)
                for key, value in values.items():
                    setattr(obj, key, value)
                obj.samples = item['samples']
                obj.conclusion = item['conclusion']
                obj.sample_size = obj.sample_size or len(item['samples'])
                obj.save()
                created += 1
                continue

            obj = PaymentWalkthrough.objects.select_for_update().get(pk=item['matched_id'])
            if item['action'] == 'skip':
                skipped += 1
                continue
            for key, value in values.items():
                if value in ('', None):
                    continue
                old = getattr(obj, key, None)
                if not overwrite and old not in ('', None):
                    continue
                setattr(obj, key, value)
            if item['samples']:
                obj.samples = item['samples']
                obj.sample_size = len(item['samples'])
            obj.conclusion = item['conclusion']
            obj.save()
            updated += 1

    return {
        'created': created, 'updated': updated, 'skipped': skipped,
        'errors': preview['errors'], 'warnings': preview['warnings'],
    }
