"""字段字典自检：声明与模型是否对得上、状态覆盖够不够。

为什么要有一个命令：字典是"AI 能看到什么"的唯一配置源，漏一条字段不会有
任何报错——模型只是默默猜错字段名；漏一条状态更糟，条件会被**静默丢掉**，
最后答一个全表合计数。这类问题只能靠定期检查发现。

    python manage.py copilot_dict_check
    python manage.py copilot_dict_check --strict   # 未声明字段也算失败
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from copilot import catalog


class Command(BaseCommand):
    help = '检查 Copilot 字段字典与模型的一致性、状态覆盖率'

    def add_arguments(self, parser):
        parser.add_argument('--strict', action='store_true',
                            help='把"未声明的业务字段"也当作错误（CI 用）')
        parser.add_argument('--module', default='',
                            help='只检查某个模块 key')
        parser.add_argument('--export', default='',
                            help='把整份字典导出为 Markdown（供业务方校对中文名）')

    def handle(self, *args, **options):
        specs = catalog.MODULES
        if options['module']:
            specs = tuple(s for s in specs if s.key == options['module'])
            if not specs:
                self.stderr.write(f'没有这个模块：{options["module"]}')
                return

        errors, warnings = [], []
        rows = []
        for spec in specs:
            missing, uncovered = catalog.field_audit(spec)
            if missing:
                errors.append(f'{spec.key}: 声明了模型上不存在的字段 {missing}')
            if uncovered:
                msg = f'{spec.key}: 未进字典的业务字段 {uncovered}'
                (errors if options['strict'] else warnings).append(msg)

            keys = [s.key for s in spec.states]
            dup = sorted({k for k in keys if keys.count(k) > 1})
            if dup:
                errors.append(f'{spec.key}: 状态 key 重复 {dup}')

            if not spec.states and not spec.no_state_reason:
                errors.append(f'{spec.key}: 没有任何状态，也没写 no_state_reason')

            rows.append((
                spec.key, spec.label, len(catalog.declared_fields(spec)),
                len(spec.states), spec.lead_days,
                len(uncovered),
            ))

        self.stdout.write('模块            名称        字段  状态  到期窗口  未声明')
        self.stdout.write('-' * 68)
        for key, label, fields, states, lead, uncovered in rows:
            self.stdout.write(
                f'{key:14} {label:10} {fields:4} {states:5} '
                f'{(str(lead) + "天") if lead else "—":>8} {uncovered:6}')
        self.stdout.write('-' * 68)
        self.stdout.write(
            f'合计：{len(rows)} 个模块，'
            f'{sum(r[3] for r in rows)} 个状态，'
            f'{sum(r[2] for r in rows)} 条字段声明')

        for line in warnings:
            self.stdout.write(self.style.WARNING(f'[警告] {line}'))
        for line in errors:
            self.stderr.write(self.style.ERROR(f'[错误] {line}'))

        if options['export']:
            path = self._export(specs, options['export'])
            self.stdout.write(f'已导出：{path}')

        if errors:
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS('字典检查通过'))

    def _export(self, specs, path):
        """导出成人能读的字典：业务方校对中文名、开发核对口径用同一份。"""
        lines = ['# Copilot 字段字典（自动生成）', '',
                 f'> 生成时间：{timezone.localtime():%Y-%m-%d %H:%M}',
                 '> 来源：`copilot/catalog.py`，请勿手工编辑，改字典后重新导出。',
                 '> 用途：这是 AI 助手"能看到什么、能按什么筛"的唯一清单。', '']
        for spec in specs:
            lines += [f'## {spec.label}（`{spec.key}`）', '',
                      f'- 模型：`{spec.model}`　数据级别：{spec.level}'
                      f'（{catalog.LEVEL_LABELS.get(spec.level, spec.level)}）']
            if spec.date_field:
                lines.append(f'- 日期口径：`{spec.date_field}`'
                             f'（{catalog.field_label(spec, spec.date_field)}）')
            if spec.lead_days:
                lines.append(f'- 到期提醒：提前 {spec.lead_days} 天')
            lines.append('')
            groups = (('可关键词匹配', spec.keyword_fields),
                      ('列表展示', spec.list_fields),
                      ('详情展示', spec.detail_fields),
                      ('可分组统计', spec.group_fields),
                      ('可求和', spec.sum_fields),
                      ('可排序', spec.order_fields))
            for title, names in groups:
                if not names:
                    continue
                cells = '、'.join(f'{catalog.field_label(spec, n)}（`{n}`）'
                                  for n in names)
                lines.append(f'- **{title}**：{cells}')
            if spec.rank_fields:
                cells = '、'.join(
                    f'{e[1]}（`{e[0]}`，别名：{"、".join(e[2:])}）'
                    for e in spec.rank_fields if e)
                lines.append(f'- **口语排序别名**：{cells}')
            if spec.sensitive_fields:
                lines.append('- **强制脱敏**：'
                             + '、'.join(catalog.field_label(spec, n)
                                        for n in spec.sensitive_fields))
            lines.append('')
            if spec.states:
                lines += ['| 状态 | 说法 | 判定口径 |', '| --- | --- | --- |']
                for state in spec.states:
                    say = '、'.join(state.phrases[:6]) or '—'
                    note = state.note or '—'
                    lines.append(f'| {state.label}（`{state.key}`） | {say} | {note} |')
            else:
                lines.append(f'该模块没有业务状态：{spec.no_state_reason or "—"}')
            lines.append('')
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write('\n'.join(lines))
        return path
