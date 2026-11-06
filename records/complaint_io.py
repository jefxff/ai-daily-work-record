"""诉讼（客诉）Excel 导入 / 导出引擎。

表头与导出严格一致（16 列），保证「导出 → 修改 → 导入」闭环。
去重键：案号（唯一业务标识）；同一文件内案号不得重复。
默认策略：仅补空（不覆盖已有值）；向导中可切换为覆盖。
事务语义：预览阶段发现任何阻断错误即整批不写入；确认阶段单事务全成全败。
"""

import io
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone

from .models import Complaint

MAX_IMPORT_ROWS = 5000
IMPORT_MAX_SIZE = 10 * 1024 * 1024

# 与视图层导出表头严格一致（顺序即导出顺序，也是模板表头）
COMPLAINT_HEADERS = [
    '序号', '纠纷类型', '原告', '被告', '案号', '涉案金额（元）', '受理法院',
    '代理律师', '进度', '立案日期', '开庭日期', '判决日期', '执行日期',
    '结案日期', '详细进度', '备注',
]

# 纯文本列：Excel 表头 -> 模型字段
TEXT_FIELDS = {
    '纠纷类型': 'dispute_type',
    '原告': 'plaintiff',
    '被告': 'defendant',
    '案号': 'case_number',
    '受理法院': 'court',
    '代理律师': 'lawyer',
    '进度': 'progress',
    '详细进度': 'detailed_progress',
    '备注': 'remarks',
}

# 金额列：Excel 表头 -> 模型字段
AMOUNT_FIELD = 'amount'
AMOUNT_HEADER = '涉案金额（元）'

# 日期列：Excel 表头 -> 模型字段
DATE_FIELDS = {
    '立案日期': 'filed_at',
    '开庭日期': 'hearing_at',
    '判决日期': 'judgment_at',
    '执行日期': 'execution_at',
    '结案日期': 'closed_at',
}

# 模型必填列（null=False, blank=False）。原告沿用历史数据可能为空，不列为阻断项。
REQUIRED_HEADERS = ['纠纷类型', '被告', '案号', AMOUNT_HEADER, '受理法院', '代理律师', '进度']

# 文本列最大长度（与模型 max_length 一致）
MAX_LENGTHS = {
    'dispute_type': (100, '纠纷类型'),
    'plaintiff': (100, '原告'),
    'defendant': (100, '被告'),
    'case_number': (50, '案号'),
    'court': (100, '受理法院'),
    'lawyer': (50, '代理律师'),
    'progress': (50, '进度'),
}

# 导入可写字段（不含序号与创建/更新时间）
IMPORT_FIELDS = [
    'dispute_type', 'plaintiff', 'defendant', 'case_number', 'amount', 'court', 'lawyer',
    'progress', 'detailed_progress', 'remarks',
    'filed_at', 'hearing_at', 'judgment_at', 'execution_at', 'closed_at',
]

# 模型字段 -> 中文标签（预览变更清单 / 导出用）
FIELD_LABELS = {field: header for header, field in TEXT_FIELDS.items()}
FIELD_LABELS[AMOUNT_FIELD] = AMOUNT_HEADER
FIELD_LABELS.update({field: header for header, field in DATE_FIELDS.items()})

_REQUIRED_HEADER = '案号'

# 历史数据里常见「暂无」等占位案号：这类值不唯一，不能作为匹配键
PLACEHOLDER_CASE_NUMBERS = {'', '暂无', '无', '-', '—', '/', 'n/a', 'na', '未填', '待定', '未立案'}

# 涉案金额 DecimalField(max_digits=10, decimal_places=2)
MAX_AMOUNT = Decimal('99999999.99')


# ---------------------------------------------------------------- 单元格解析

def _cell_text(value):
    """任意单元格值 -> 去空白字符串（None -> ''，日期按 YYYY-MM-DD）。"""
    if value is None:
        return ''
    if isinstance(value, datetime):
        return value.strftime('%Y-%m-%d')
    if isinstance(value, date):
        return value.strftime('%Y-%m-%d')
    return str(value).strip()


def _to_decimal(raw):
    """金额原文 -> 元 Decimal；无法结构化时返回 None。"""
    text = _cell_text(raw).replace(',', '').replace(' ', '')
    if not text:
        return None
    if text.endswith('元'):
        text = text[:-1]
    try:
        return Decimal(text).quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError):
        return None


def _to_date(raw):
    """日期单元格 -> date；无法识别返回 None。"""
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    text = _cell_text(raw)
    if not text:
        return None
    text = text.split(' ')[0]
    for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%Y.%m.%d', '%Y%m%d'):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _empty(value):
    return value is None or value == ''


# ---------------------------------------------------------------- 文件解析

def _validate_file(f):
    from work_record.file_validation import content_matches, normalize_ext, read_head

    ext = normalize_ext(getattr(f, 'name', '') or '')
    if ext not in {'XLSX', 'XLS'}:
        raise ValueError(
            f'不支持的文件类型「{ext or "无扩展名"}」；诉讼导入仅支持 .xlsx / .xls'
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
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    if not rows:
        return [], []
    headers = [_cell_text(h) for h in rows[0]]
    data_rows = []
    for raw in rows[1:]:
        if all(v is None or _cell_text(v) == '' for v in raw):
            continue
        data_rows.append({
            header: _cell_text(value)
            for header, value in zip(headers, raw) if header
        })
    return headers, data_rows


def _read_xls(f):
    try:
        import xlrd
    except ImportError:
        raise ValueError('服务器缺少 xlrd 依赖，暂时无法解析 .xls；请另存为 .xlsx 后重试')

    wb = xlrd.open_workbook(file_contents=f.read())
    sheet = wb.sheet_by_index(0)
    if sheet.nrows < 2:
        return [], []
    headers = [_cell_text(sheet.cell_value(0, c)) for c in range(sheet.ncols)]
    data_rows = []
    for r in range(1, sheet.nrows):
        row = {}
        for c in range(sheet.ncols):
            header = headers[c]
            if not header:
                continue
            cell = sheet.cell(r, c)
            if cell.ctype == xlrd.XL_CELL_DATE:
                try:
                    value = xlrd.xldate_as_datetime(cell.value, wb.datemode)
                    row[header] = _cell_text(value)
                except (ValueError, OverflowError):
                    row[header] = _cell_text(cell.value)
            else:
                row[header] = _cell_text(cell.value)
        if any(v for v in row.values()):
            data_rows.append(row)
    return headers, data_rows


def _parse_excel(f):
    """解析 Excel，返回 (原始表头, 数据行)；表头与行数校验在此完成。"""
    ext = _validate_file(f)
    try:
        f.seek(0)
    except (AttributeError, OSError):
        pass
    raw_headers, rows = _read_xls(f) if ext == 'XLS' else _read_xlsx(f)
    headers = [h for h in raw_headers if h]
    if _REQUIRED_HEADER not in headers:
        raise ValueError('Excel 缺少必填列「案号」，请使用系统提供的导入模板')
    if len(rows) > MAX_IMPORT_ROWS:
        raise ValueError(f'单次导入最多 {MAX_IMPORT_ROWS} 行，当前 {len(rows)} 行')
    return raw_headers, rows


def parse_excel(f):
    """解析 Excel，返回 [{表头: 文本}]；表头缺失/行数超限时抛 ValueError。"""
    return _parse_excel(f)[1]


def parse_excel_with_headers(f):
    """同 parse_excel，额外返回原始表头（含空表头列），供预览页做列检查。"""
    return _parse_excel(f)


# ---------------------------------------------------------------- 表头列检查

def _col_letter(index):
    """1-based 列序号 -> Excel 列字母（1 -> A，27 -> AA）。"""
    letters = ''
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def build_header_report(raw_headers):
    """表头列检查：缺失列 / 未识别列 / 空表头列 / 列顺序。

    只生成展示数据，不改变解析与校验口径（系统按表头名取值）。
    """
    present = [h for h in raw_headers if h]
    expected = list(COMPLAINT_HEADERS)
    known = [h for h in present if h in expected]
    unknown = [h for h in present if h not in expected]
    missing_required = [h for h in REQUIRED_HEADERS if h not in present]
    missing_optional = [h for h in expected if h not in present and h not in REQUIRED_HEADERS]
    blank_columns = [_col_letter(i) for i, h in enumerate(raw_headers, 1) if not h]
    order_mismatch = known != [h for h in expected if h in present]

    issues = []
    if missing_required:
        issues.append(('danger', '缺失必填列：' + '、'.join(missing_required)))
    if unknown:
        issues.append(('warning', '未识别列：' + '、'.join(unknown) + '（导入时忽略）'))
    if blank_columns:
        issues.append((
            'warning',
            f'空表头列 {len(blank_columns)} 个（{"、".join(blank_columns)} 列），导入时忽略',
        ))
    if missing_optional:
        issues.append(('warning', '缺少可选列：' + '、'.join(missing_optional)))
    if order_mismatch:
        issues.append(('warning', '列顺序与模板不同；系统按表头名取值，不影响导入'))

    return {
        'total_columns': len(raw_headers),
        'recognized': known,
        'recognized_count': len(known),
        'unknown': unknown,
        'missing_required': missing_required,
        'missing_optional': missing_optional,
        'blank_columns': blank_columns,
        'order_mismatch': order_mismatch,
        'issues': issues,
        'ok': not issues,
    }


# ---------------------------------------------------------------- 行 -> 字段

def row_to_values(row):
    """Excel 行 -> 模型字段字典（含金额结构化值、日期）。"""
    values = {}
    for header, field in TEXT_FIELDS.items():
        values[field] = _cell_text(row.get(header))
    values[AMOUNT_FIELD] = _to_decimal(row.get(AMOUNT_HEADER))
    for header, field in DATE_FIELDS.items():
        values[field] = _to_date(row.get(header))
    return values


def _display(value):
    if value is None:
        return ''
    if isinstance(value, Decimal):
        return format(value.normalize(), 'f')
    if isinstance(value, (date, datetime)):
        return value.strftime('%Y-%m-%d')
    return str(value)


def _changes(values, obj, overwrite):
    """计算本行将写入的字段 [(字段标签, 新值)]；obj 为 None 表示新建。"""
    changes = []
    for field in IMPORT_FIELDS:
        new = values.get(field)
        if _empty(new):
            continue
        if obj is not None:
            old = getattr(obj, field)
            if not overwrite and not _empty(old):
                continue
            if old == new:
                continue
        changes.append((FIELD_LABELS.get(field, field), _display(new)))
    return changes


def _row_error_items(index, values, row):
    """单行阻断错误的结构化列表 [(列名, 异常类型, 完整文案)]。

    完整文案与改造前 _row_errors 逐字一致；按列汇总与逐行清单共用同一来源，
    避免展示层再靠字符串猜列名。
    """
    items = []
    for header in REQUIRED_HEADERS:
        if header == AMOUNT_HEADER:
            continue
        field = TEXT_FIELDS.get(header)
        if _empty(values.get(field)):
            items.append((header, '为空', f'第 {index} 行：{header}为空'))
    for field, (limit, label) in MAX_LENGTHS.items():
        text = values.get(field) or ''
        if len(text) > limit:
            items.append((label, '超长', f'第 {index} 行：{label}超过 {limit} 个字符'))
    progress = values.get('progress')
    if progress and progress not in [c[0] for c in Complaint.PROGRESS_CHOICES]:
        allowed = ' / '.join(c[0] for c in Complaint.PROGRESS_CHOICES)
        items.append((
            '进度', '枚举不符',
            f'第 {index} 行：进度「{progress}」不是有效状态（可选：{allowed}）',
        ))
    # 金额单独判：空单元格报「为空」，有原文却解析不出数值报「无法识别」，两者不重复
    raw_amount = _cell_text(row.get(AMOUNT_HEADER))
    amount = values.get(AMOUNT_FIELD)
    if not raw_amount:
        items.append((AMOUNT_HEADER, '为空', f'第 {index} 行：{AMOUNT_HEADER}为空'))
    elif amount is None:
        items.append((AMOUNT_HEADER, '无法识别为数值', f'第 {index} 行：涉案金额无法识别为数值'))
    if amount is not None and (amount > MAX_AMOUNT or amount < 0):
        items.append((
            AMOUNT_HEADER, '超出范围',
            f'第 {index} 行：涉案金额 {amount} 超出允许范围（0 ~ {MAX_AMOUNT}）',
        ))
    return items


def _row_errors(index, values, row):
    """单行阻断错误列表（必填 / 长度 / 枚举 / 金额）。"""
    return [text for _column, _kind, text in _row_error_items(index, values, row)]


def _compress_rows(numbers):
    """行号列表压成区间文案：[15,16,18,28..45] -> '15、16、18、28-45'。"""
    if not numbers:
        return ''
    parts = []
    start = prev = numbers[0]
    for num in numbers[1:]:
        if num == prev + 1:
            prev = num
            continue
        parts.append(str(start) if start == prev else f'{start}-{prev}')
        start = prev = num
    parts.append(str(start) if start == prev else f'{start}-{prev}')
    return '、'.join(parts)


def _summarize_column_issues(issue_rows):
    """按 (列名, 异常类型) 汇总，供预览页顶部展示「哪些列异常」。"""
    groups = {}
    for item in issue_rows:
        key = (item['column'], item['kind'])
        group = groups.setdefault(key, {
            'column': item['column'], 'kind': item['kind'],
            'count': 0, 'rows': [],
        })
        group['count'] += 1
        group['rows'].append(item['row'])
    order = {header: i for i, header in enumerate(COMPLAINT_HEADERS)}
    result = []
    for group in groups.values():
        group['row_label'] = _compress_rows(sorted(set(group['rows'])))
        result.append(group)
    result.sort(key=lambda g: (-g['count'], order.get(g['column'], 999), g['kind']))
    return result


# ---------------------------------------------------------------- 预览

def _match_spec(values):
    """返回 (匹配依据标签, 查询条件字典, 文件内去重键)。

    案号为有效值时按案号唯一匹配；「暂无」这类占位案号改用
    「被告 + 纠纷类型」（二者组合可重复，故不作为文件内阻断键）。
    """
    case_number = (values.get('case_number') or '').strip()
    if case_number.lower() not in PLACEHOLDER_CASE_NUMBERS:
        return '案号', {'case_number': case_number}, ('案号', case_number)
    defendant = values.get('defendant') or ''
    dispute_type = values.get('dispute_type') or ''
    return (
        '被告+纠纷类型',
        {'defendant': defendant, 'dispute_type': dispute_type},
        ('组合', f'{defendant}|{dispute_type}'),
    )


def build_preview(rows, overwrite=False):
    """构建导入预览：items / errors / warnings / summary / column_issues。

    errors 为阻断错误（确认阶段整批拒绝）；warnings 为不影响导入的提示。
    column_issues / issue_rows 只用于预览页展示，不参与校验口径。
    """
    items, errors, warnings = [], [], []
    issue_rows = []
    seen_case = {}

    parsed = []
    for index, row in enumerate(rows, start=2):
        values = row_to_values(row)
        error_items = _row_error_items(index, values, row)
        for column, kind, text in error_items:
            issue_rows.append({
                'row': index, 'column': column, 'kind': kind,
                'text': text, 'reason': text.split('：', 1)[-1],
            })
        parsed.append((index, values, [text for _c, _k, text in error_items]))

    # 组合键（占位案号行）在文件内出现次数：同组多行无法区分，需跳过
    combo_counts = {}
    for index, values, row_errors in parsed:
        if row_errors:
            continue
        matched_by, _lookup, dedupe_key = _match_spec(values)
        if matched_by == '组合':
            combo_counts[dedupe_key] = combo_counts.get(dedupe_key, 0) + 1

    for index, values, row_errors in parsed:
        if row_errors:
            errors.extend(row_errors)
            continue

        matched_by, lookup, dedupe_key = _match_spec(values)
        if matched_by == '案号':
            if dedupe_key in seen_case:
                text = f'第 {index} 行：案号「{dedupe_key[1]}」与第 {seen_case[dedupe_key]} 行重复'
                errors.append(text)
                issue_rows.append({
                    'row': index, 'column': '案号', 'kind': '文件内重复',
                    'text': text, 'reason': text.split('：', 1)[-1],
                })
                continue
            seen_case[dedupe_key] = index

        matches = Complaint.objects.filter(**lookup)
        obj = matches.first()
        match_count = matches.count()
        # 占位案号无法唯一定位：系统内多条或文件内同组多行时宁可不写，避免错写
        ambiguous = matched_by == '组合' and (
            match_count > 1 or combo_counts.get(dedupe_key, 0) > 1
        )
        if ambiguous:
            warnings.append(
                f'第 {index} 行：案号为占位值，且「{dedupe_key[1]}」'
                f'在{"系统" if match_count > 1 else "本次文件"}中有多条，已跳过（请手工核对）'
            )
            items.append({
                'row': index,
                'action': 'skip',
                'values': values,
                'case_number': values.get('case_number', ''),
                'defendant': values.get('defendant', ''),
                'matched_by': matched_by,
                'matched_id': None,
                'changes': [],
            })
            continue

        changes = _changes(values, obj, overwrite)
        if obj is None:
            action = 'create'
        elif changes:
            action = 'update'
        else:
            action = 'skip'
        items.append({
            'row': index,
            'action': action,
            'values': values,
            'case_number': values.get('case_number', ''),
            'defendant': values.get('defendant', ''),
            'matched_by': matched_by if obj is not None else '',
            'matched_id': getattr(obj, 'pk', None),
            'changes': changes,
        })

    summary = {
        'create': sum(1 for i in items if i['action'] == 'create'),
        'update': sum(1 for i in items if i['action'] == 'update'),
        'skip': sum(1 for i in items if i['action'] == 'skip'),
        'error': len(errors),
    }
    missing_plaintiff = sum(
        1 for i in items
        if i['action'] in ('create', 'update')
        and not (i['values'].get('plaintiff') or '').strip()
    )
    if missing_plaintiff:
        warnings.insert(
            0,
            f'共 {missing_plaintiff} 行原告为空，导入后请在编辑页补填（不影响本次导入）',
        )
    return {
        'items': items,
        'errors': errors,
        'warnings': warnings,
        'summary': summary,
        'column_issues': _summarize_column_issues(issue_rows),
        'issue_rows': issue_rows,
    }


# ---------------------------------------------------------------- 写入

def apply_rows(rows, overwrite=False):
    """按预览结果写库：先把阻断错误全部拦下，再单事务全成全败。"""
    preview = build_preview(rows, overwrite=overwrite)
    if preview['errors']:
        raise ValueError('导入数据校验未通过：' + '；'.join(preview['errors'][:10]))

    created = updated = skipped = 0
    with transaction.atomic():
        for item in preview['items']:
            values = item['values']
            if item['action'] == 'create':
                Complaint(**values).save()
                created += 1
                continue
            if item['action'] == 'skip':
                skipped += 1
                continue
            obj = Complaint.objects.select_for_update().get(pk=item['matched_id'])
            touched = False
            for field in IMPORT_FIELDS:
                new = values.get(field)
                if _empty(new):
                    continue
                old = getattr(obj, field)
                if not overwrite and not _empty(old):
                    continue
                if old == new:
                    continue
                setattr(obj, field, new)
                touched = True
            if touched:
                obj.save()
                updated += 1
            else:
                skipped += 1
    return {
        'created': created,
        'updated': updated,
        'skipped': skipped,
        'errors': preview['errors'],
        'warnings': preview['warnings'],
    }


# ---------------------------------------------------------------- 导出 / 模板

def complaint_export_row(obj, serial):
    """单条记录 -> 16 列导出值（与导入表头严格对齐）。"""
    return [
        serial,
        obj.dispute_type, obj.plaintiff, obj.defendant, obj.case_number, _display(obj.amount),
        obj.court, obj.lawyer, obj.progress,
        _display(obj.filed_at), _display(obj.hearing_at), _display(obj.judgment_at),
        _display(obj.execution_at), _display(obj.closed_at),
        obj.detailed_progress or '', obj.remarks or '',
    ]


def template_response():
    """下载导入模板：16 列表头 + 填写说明工作表（不含示例数据，避免误导入）。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    from work_record.export_utils import content_disposition, sanitize_xlsx_formulas

    wb = Workbook()
    ws = wb.active
    ws.title = '诉讼记录'
    ws.append(COMPLAINT_HEADERS)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill('solid', fgColor='E6F7FF')
        cell.alignment = Alignment(wrap_text=True, vertical='center')
    for col in range(1, len(COMPLAINT_HEADERS) + 1):
        ws.column_dimensions[ws.cell(row=1, column=col).column_letter].width = 18

    notes = wb.create_sheet('填写说明')
    notes.append(['填写说明'])
    notes['A1'].font = Font(bold=True, size=14)
    progress_text = ' / '.join(c[0] for c in Complaint.PROGRESS_CHOICES)
    for line in [
        '1. 仅支持 .xlsx / .xls，单次最多 5000 行；「案号」为必填列，也是去重键。',
        f'2. 必填列：{"、".join(REQUIRED_HEADERS)}。',
        f'3. 进度只能填：{progress_text}。',
        '4. 默认仅补空，不覆盖系统已有值；导入向导中可切换为覆盖。',
        '5. 涉案金额单位为元，可填「120000」「120000.50」；不要带千分位逗号以外的文字。',
        '6. 日期支持 2026-01-31、2026/1/31、20260131；无法识别时留空。',
        '7. 「序号」列仅用于阅读，导入时忽略。',
        '8. 同一文件内案号不得重复；同一案号在系统中存在多条时，仅更新最早一条。',
    ]:
        notes.append([line])
    notes.column_dimensions['A'].width = 80

    sanitize_xlsx_formulas(wb)
    buf = io.BytesIO()
    wb.save(buf)
    filename = f'诉讼导入模板-{timezone.localtime().strftime("%Y%m%d")}.xlsx'
    response = HttpResponse(
        buf.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = content_disposition(
        filename, ascii_fallback='complaint-import-template.xlsx'
    )
    return response
