"""全模块问句矩阵评估：把每个模块的常见问法都跑一遍，逐条核对答案。

这里**不是**训练模型——LLM 全程不参与。前提是：能确定性回答的问题就该由
本地链路答对，模型只兜长尾。评估只盯三件事：

1. **识别**：问句落到正确的模块（过去的 bug 几乎全在这里：答成别的模块、
   或退化成"没有记录"）；
2. **对数**：答案里的数字与数据库直接统计一致（"全表数字冒充条件结果"
   就是这么漏出去的）；
3. **可读**：答案是有结构的 Markdown（加粗结论 + 列表/表格），不是一坨纯文本。

新增模块、改别名表之后跑一遍 ``manage.py copilot_eval``，红的就是能力缺口。
"""

from dataclasses import dataclass, field

from django.utils import timezone

from . import catalog, markdown_render, registry, security
from .engine import planner
from .tools.common import apply_filters, base_queryset


@dataclass
class Case:
    module: str
    module_label: str
    kind: str                 # list / state / count / group / value
    question: str
    expect: str
    actual: str = ''
    ok: bool = False
    skipped: bool = False
    detail: str = ''


@dataclass
class Report:
    cases: list = field(default_factory=list)
    modules: list = field(default_factory=list)

    @property
    def total(self):
        return len(self.cases)

    @property
    def skipped(self):
        return sum(1 for c in self.cases if c.skipped)

    @property
    def passed(self):
        return sum(1 for c in self.cases if c.ok and not c.skipped)

    @property
    def failed(self):
        return sum(1 for c in self.cases if not c.ok and not c.skipped)

    @property
    def rate(self):
        counted = self.total - self.skipped
        return (self.passed / counted * 100) if counted else 100.0

    def per_module(self):
        rows = []
        for spec_key, spec_label in self.modules:
            items = [c for c in self.cases if c.module == spec_key]
            if not items:
                continue
            skipped = sum(1 for c in items if c.skipped)
            counted = len(items) - skipped
            ok = sum(1 for c in items if c.ok and not c.skipped)
            rows.append({
                'module': spec_key, 'label': spec_label,
                'total': len(items), 'passed': ok, 'skipped': skipped,
                'rate': (ok / counted * 100) if counted else 100.0,
            })
        return rows


def _ctx(user, policy):
    return registry.ToolContext(
        user=user, conversation=None, run=None, is_local=True, policy=policy)


def _query(ctx, module, state=None, keyword='', limit=10):
    args = {'module': module, 'limit': limit}
    if state:
        args['state'] = state
    if keyword:
        args['keyword'] = keyword
    return registry.get('query_records').handler(ctx, **args)


def _has_markdown(text):
    """有结构才算"排版过"：加粗、列表或表格，占一样即可。"""
    return any(token in (text or '') for token in ('**', '\n- ', '\n| '))


def _distinct_values(spec, qs, field_name, limit=8):
    """取该字段的真实取值：优先选项字段，否则用前几个不同的值。"""
    try:
        values = list(qs.values_list(field_name, flat=True).distinct()[:limit])
    except Exception:  # noqa: BLE001 - 关联字段取不了就跳过
        return []
    return [v for v in values if v not in (None, '')]


def _list_cases(spec, qs, total, modules, user, ctx, report):
    unit = planner.unit_of(spec)
    question = f'{spec.label}有哪些'
    case = Case(spec.key, spec.label, 'list', question,
                f'识别为「{spec.label}」并列出 {total} {unit}')
    found, keyword, _state = planner.module_query(question, modules, user)
    if found is None or found.key != spec.key:
        case.actual = f'识别为「{found.label if found else "（未识别，交给模型）"}」'
        report.cases.append(case)
        return
    result = _query(ctx, spec.key, keyword=keyword)
    data = result.data or {}
    case.actual = (f'keyword={keyword!r} total={data.get("total")} '
                   f'rows={result.row_count}')
    answer = planner.module_digest(
        spec, data.get('rows') or [], data.get('total') or 0,
        data.get('columns') or {}, keyword=keyword)
    checks = [
        bool(result.ok),
        (data.get('total') or 0) == total,
        f'共 **{total} {unit}**' in answer,
        _has_markdown(answer),
    ]
    if total and not (data.get('rows') or []):
        checks.append(False)          # 有数据却一行没取到
    case.ok = all(checks)
    case.detail = '' if case.ok else f'checks={checks}'
    case.actual += f' | 首行：{answer.splitlines()[0][:40] if answer else "（空）"}'
    report.cases.append(case)


def _state_cases(spec, qs, modules, user, ctx, report):
    unit = planner.unit_of(spec)
    for state in spec.states:
        expect = qs.filter(state.filter_q(timezone.localdate())).count()
        question = f'{spec.label}中{state.label}的有哪些'
        case = Case(spec.key, spec.label, 'state', question,
                    f'状态「{state.label}」共 {expect} {unit}')
        found, _keyword, got_state = planner.module_query(question, modules, user)
        if found is None or found.key != spec.key:
            case.actual = f'识别为「{found.label if found else "（未识别）"}」'
            report.cases.append(case)
            continue
        if got_state is None or got_state.key != state.key:
            case.actual = f'状态识别为 {got_state.label if got_state else "（无）"}'
            report.cases.append(case)
            continue
        result = _query(ctx, spec.key, state=state.key)
        data = result.data or {}
        answer = planner.module_digest(
            spec, data.get('rows') or [], data.get('total') or 0,
            data.get('columns') or {}, state_label=state.label)
        case.actual = f'total={data.get("total")}（应 {expect}）'
        if expect == 0:
            # 0 条时回答是"没有符合条件的记录"（纯文本，不带排版），
            # 这里只认这个措辞，不要求 Markdown
            case.ok = ((data.get('total') or 0) == 0
                       and '没有符合条件的记录' in answer)
        else:
            # 状态没生效时数字会等于全表：这一条能抓住"状态被忽略"的退化
            case.ok = ((data.get('total') or 0) == expect
                       and f'共 **{expect} {unit}**' in answer
                       and _has_markdown(answer))
        report.cases.append(case)


def _count_cases(spec, qs, total, modules, user, report):
    unit = planner.unit_of(spec)
    question = f'{spec.label}一共有多少{unit}？'
    case = Case(spec.key, spec.label, 'count', question, f'共 {total} {unit}')
    stat = planner.stat_query(question, modules, user)
    if stat is None or stat['spec'].key != spec.key:
        case.actual = f'识别为「{stat["spec"].label if stat else "（未识别）"}」'
        report.cases.append(case)
        return
    answer, _refs = planner.stat_answer(stat, user)
    case.actual = (answer or '').splitlines()[0][:60]
    case.ok = (f'**{total} {unit}**' in (answer or '')
               and _has_markdown(answer))
    report.cases.append(case)


def _group_cases(spec, qs, total, modules, user, report):
    if not spec.group_fields:
        return
    unit = planner.unit_of(spec)
    group_field = spec.group_fields[0]
    label = catalog.field_label(spec, group_field)
    question = f'{spec.label}按{label}分别有多少？'
    case = Case(spec.key, spec.label, 'group', question,
                f'按「{label}」分组，合计 {total} {unit}')
    stat = planner.stat_query(question, modules, user)
    if stat is None or stat['spec'].key != spec.key:
        case.actual = f'识别为「{stat["spec"].label if stat else "（未识别）"}」'
        report.cases.append(case)
        return
    if stat.get('group_field') != group_field:
        case.actual = f'分组字段为 {stat.get("group_field")!r}，应为 {group_field!r}'
        report.cases.append(case)
        return
    answer, _refs = planner.stat_answer(stat, user)
    case.actual = (answer or '').splitlines()[0][:60]
    case.ok = (f'**{total} {unit}**' in (answer or '')
               and '| --- |' in (answer or ''))
    report.cases.append(case)


def _alias_cases(spec, modules, user, report):
    """每种口语说法都问一遍：「营业执照」「资质证书」「审厂」…

    用户不会照着 label 提问，别名表就是"他能听懂哪些叫法"。这条最容易在
    改别名表时被悄悄弄坏，所以逐条断言。
    """
    aliases = [word for word in planner.MODULE_ALIASES.get(spec.key, ())
               if word and len(word) >= 2 and word not in planner.GENERIC_NAME_WORDS]
    for alias in aliases:
        question = f'{alias}有哪些'
        case = Case(spec.key, spec.label, 'alias', question, f'「{alias}」→「{spec.label}」')
        found = planner.pick_module(question, modules, user)
        case.ok = found is not None and found.key == spec.key
        case.actual = found.label if found else '未识别'
        report.cases.append(case)


def _is_text_field(spec, field_name):
    """只对文本字段生成"按值筛选"用例。

    布尔/数字字段（"是否下单=True"）不是业务同事会说的话，生成出来也只是
    给自己的评估添噪声；真要说，也该说"已下单的"。
    """
    try:
        field = catalog.model_of(spec)._meta.get_field(field_name)
    except Exception:  # noqa: BLE001
        return False
    return field.get_internal_type() in ('CharField', 'TextField')


def _value_cases(spec, qs, total, modules, user, report):
    """按字段真实取值提问：这是最容易被"全表数字"糊过去的一类。"""
    if not spec.group_fields:
        return
    unit = planner.unit_of(spec)
    made = 0
    for group_field in spec.group_fields:
        if made >= 2:                 # 每模块最多两个字段，够了
            break
        if not _is_text_field(spec, group_field):
            continue
        values = _distinct_values(spec, qs, group_field)
        if not 2 <= len(values) <= 8:
            continue
        # 选一个"部分命中"的值：命中全部的话，答错也看不出来
        picked, expect = None, 0
        for value in values:
            n = qs.filter(**{group_field: value}).count()
            if 0 < n < total:
                picked, expect = value, n
                break
        if picked is None:
            continue
        label = catalog.label_for_value(spec, group_field, picked)
        field_label = catalog.field_label(spec, group_field)
        question = f'{spec.label}中{field_label}为{label}的有多少？'
        case = Case(spec.key, spec.label, 'value', question,
                    f'筛选「{field_label}={label}」后共 {expect} {unit}')
        stat = planner.stat_query(question, modules, user)
        if stat is None or stat['spec'].key != spec.key:
            case.actual = f'识别为「{stat["spec"].label if stat else "（未识别）"}」'
            report.cases.append(case)
            continue
        answer, _refs = planner.stat_answer(stat, user)
        case.actual = (answer or '').splitlines()[0][:60]
        case.ok = (f'**{expect} {unit}**' in (answer or '')
                   and bool(stat.get('keyword')))
        report.cases.append(case)
        made += 1


def _bank_cases(user, modules, report):
    """手写问句库（``question_bank``）逐条过一遍。

    和生成矩阵的分工：矩阵验证"每种问法类型都答得对"，问句库验证
    "业务同事真会这么说时也认得出"。后者更口语，且是新增模块时必须补的样本。
    """
    from .question_bank import QUESTIONS

    by_key = {spec.key: spec for spec in modules}
    for key, questions in QUESTIONS.items():
        spec = by_key.get(key)
        if spec is None:
            continue          # 当前账号无权限，不评估
        for question in questions:
            case = Case(key, spec.label, 'bank', question,
                        f'识别为「{spec.label}」')
            found = planner.pick_module(question, modules, user)
            case.ok = found is not None and found.key == key
            # 顺带标出"这条能不能本地答"，报告里一眼看出还有多少要靠模型
            stat = planner.stat_query(question, modules, user)
            if stat is not None and stat['spec'].key == key:
                mode = '本地统计'
            else:
                spec_hit, _kw, _st = planner.module_query(question, modules, user)
                mode = '本地列表' if (spec_hit is not None and spec_hit.key == key) else '交模型'
            case.actual = f'{found.label if found else "未识别"} · {mode}'
            report.cases.append(case)


def run_all(user, policy=None, modules=None):
    """跑一遍全模块矩阵，返回 ``Report``。"""
    policy = policy or security.policy()
    modules = modules or registry.available_modules(user, True, policy)
    ctx = _ctx(user, policy)
    report = Report(modules=[(m.key, m.label) for m in modules])
    for spec in modules:
        qs = base_queryset(spec, user)
        try:
            total = qs.count()
        except Exception:  # noqa: BLE001 - 模型/表缺失时跳过而不是整轮崩掉
            report.cases.append(Case(spec.key, spec.label, 'list',
                                     f'{spec.label}有哪些', '可查询',
                                     skipped=True, detail='表不可查询'))
            continue
        if total == 0:
            report.cases.append(Case(spec.key, spec.label, 'list',
                                     f'{spec.label}有哪些', '可查询',
                                     skipped=True, detail='演示库为空'))
            continue
        _list_cases(spec, qs, total, modules, user, ctx, report)
        _alias_cases(spec, modules, user, report)
        _state_cases(spec, qs, modules, user, ctx, report)
        _count_cases(spec, qs, total, modules, user, report)
        _group_cases(spec, qs, total, modules, user, report)
        _value_cases(spec, qs, total, modules, user, report)
    _bank_cases(user, modules, report)
    return report


def render_markdown(report, user=None, generated_at=None):
    """评估报告（Markdown）：总览 → 每模块通过率 → 失败明细。"""
    stamp = (generated_at or timezone.localtime()).strftime('%Y-%m-%d %H:%M')
    lines = [
        '# AI Copilot 全模块问句评估报告', '',
        f'- 生成时间：{stamp}',
        f'- 账号：{user.get_username() if user else "-"}',
        f'- 模块：**{len(report.modules)}** 个',
        f'- 用例：**{report.total}**（跳过 {report.skipped}）',
        f'- 通过：**{report.passed}**，失败：**{report.failed}**',
        f'- 通过率：**{report.rate:.1f}%**', '',
        '> 评估全程不调用大模型：只看本地确定性链路能否答对。'
        '失败项即"这类问法现在会答错或答不出"。', '',
        '## 各模块通过率', '',
        '| 模块 | 用例 | 通过 | 跳过 | 通过率 |',
        '| --- | --- | --- | --- | --- |',
    ]
    for row in report.per_module():
        lines.append(f"| {row['label']} | {row['total']} | {row['passed']} "
                     f"| {row['skipped']} | {row['rate']:.0f}% |")

    failed = [c for c in report.cases if not c.ok and not c.skipped]
    lines += ['', f'## 失败明细（{len(failed)} 条）', '']
    if not failed:
        lines.append('无。')
    else:
        lines += ['| 模块 | 类型 | 问句 | 期望 | 实际 |', '| --- | --- | --- | --- | --- |']
        for case in failed:
            lines.append(
                f"| {case.module_label} | {case.kind} | {case.question} "
                f"| {case.expect} | {case.actual or '-'} |")

    skipped = [c for c in report.cases if c.skipped]
    if skipped:
        lines += ['', f'## 跳过（{len(skipped)} 条）', '']
        lines += [f'- {c.module_label}：{c.detail}' for c in skipped]
    return '\n'.join(lines) + '\n'
