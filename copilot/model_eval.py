"""模型链路评估：真实跑一遍模型，回答"长尾问法现在到底行不行"。

和 ``eval_matrix`` 的分工：
- ``eval_matrix``：本地确定性链路，0 调用，303 条，跑在每次提交前；
- 本模块：真实调用模型，60 条，只在换模型/改提示词/改工具签名时跑。

判定三重（都可机械验证，不猜措辞）：
1. **工具**：实际调用的工具与期望有交集（识别对不对）；
2. **范围**：引用里出现了目标模块，或答案提到了模块名（有没有查错地方）；
3. **要点/禁词**：必须出现的词至少命中一个，禁止出现的推脱话术一个都不能有。

同一题跑 2 次（可配），**两次都过才算过**——小模型抖动大，一次侥幸通过
会把基线做虚。抖动率单独统计，换模型时先看这个数。
"""

import time
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from . import catalog, security
from .engine import planner
from .engine.executor import execute_run
from .models import CopilotConversation, CopilotPolicy, CopilotRun
from .model_cases import MODEL_CASES, ModelCase


@dataclass
class ModelResult:
    question: str
    ok: bool = False
    attempts: list = field(default_factory=list)   # 每次的 Attempt
    failure: str = ''
    note: str = ''


@dataclass
class Attempt:
    status: str = ''
    tools: list = field(default_factory=list)
    answer: str = ''
    tokens: int = 0
    steps: int = 0
    latency: float = 0.0
    error_code: str = ''
    checks: dict = field(default_factory=dict)
    ok: bool = False


@dataclass
class ModelReport:
    results: list = field(default_factory=list)
    provider: str = ''
    model: str = ''
    is_local: bool = True
    generated_at: object = None
    rounds: int = 2            # 每题跑几次

    @property
    def total(self):
        return len(self.results)

    @property
    def passed(self):
        return sum(1 for r in self.results if r.ok)

    @property
    def rate(self):
        return (self.passed / self.total * 100) if self.total else 100.0

    def attempts(self):
        for result in self.results:
            for attempt in result.attempts:
                yield attempt

    @property
    def flaky(self):
        """两次结果不一致的题目数（一次过一次不过）。"""
        return sum(1 for r in self.results
                   if len({a.ok for a in r.attempts}) > 1)

    @property
    def flaky_rate(self):
        return (self.flaky / self.total * 100) if self.total else 0.0

    def avg(self, attr):
        values = [getattr(a, attr) for a in self.attempts()]
        return (sum(values) / len(values)) if values else 0

    def failure_counts(self):
        counts = {}
        for result in self.results:
            if result.ok:
                continue
            key = result.failure or '其它'
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def _module_words(scope):
    """目标模块的判断词：label、别名、以及"能说明查的是它"的关键词。"""
    spec = catalog.get_module(scope)
    if spec is None:
        return []
    words = [spec.label]
    words.extend(planner.MODULE_ALIASES.get(scope, ()))
    return [w for w in words if w]


def _verdict(case, run, answer):
    tools = {s.get('tool') for s in (run.steps or []) if s.get('tool')}
    checks = {
        'tool': (not case.tools) or bool(tools & set(case.tools)),
        'answer': bool((answer or '').strip()) and run.status == CopilotRun.Status.SUCCEEDED,
        'forbidden': not any(word in (answer or '') for word in case.forbid),
    }
    if case.scope:
        refs = {r.get('module') for r in (run.refs or []) if r.get('module')}
        words = _module_words(case.scope)
        checks['scope'] = (case.scope in refs) or any(w in (answer or '') for w in words)
    else:
        checks['scope'] = True
    if case.keywords:
        checks['keywords'] = any(w in (answer or '') for w in case.keywords)
    else:
        checks['keywords'] = True
    return checks


def _failure_of(checks, tools):
    if 'egress_blocked' in tools:
        return '外发被拒'
    if not checks['answer']:
        return '空回答或失败'
    if not checks['tool']:
        return '工具调错（识别问题）'
    if not checks['scope']:
        return '查错模块'
    if not checks['keywords']:
        return '答案偏离要点'
    if not checks['forbidden']:
        return '推脱话术'
    return '其它'


def _ask(user, question):
    """真实跑一轮：建会话 → 同步执行 → 收集结果 → 归档会话。"""
    conversation = CopilotConversation.objects.create(
        user=user, title=f'[评估] {question[:50]}')
    policy = CopilotPolicy.current()
    run = CopilotRun.objects.create(
        conversation=conversation, user=user, question=question,
        deadline_at=timezone.now() + timedelta(
            seconds=getattr(settings, 'COPILOT_RUN_TIMEOUT', 120)),
    )
    started = time.time()
    try:
        execute_run(run.pk)
    except Exception:  # noqa: BLE001 - 单题异常不能让整轮评估中断
        pass
    latency = time.time() - started
    run.refresh_from_db()
    # 归档而不是删除：token/时延要进用量统计，但会话列表不该被 60 条
    # "[评估]" 撑满。归档会话 30 天后由 copilot_prune 清掉。
    CopilotConversation.objects.filter(pk=conversation.pk).update(
        status=CopilotConversation.Status.ARCHIVED, archived_at=timezone.now())
    return run, latency, policy


def run_cases(user, cases=None, rounds=2, limit=0):
    from .providers import router
    cases = list(cases or MODEL_CASES)
    if limit:
        cases = cases[:limit]
    try:
        config, _provider = router.select('chat')
    except Exception:  # noqa: BLE001 - 没配模型也要出报告，而不是崩掉
        config = None

    report = ModelReport(
        provider=getattr(config, 'provider', '') or '',
        model=getattr(config, 'model', '') or '',
        is_local=bool(getattr(config, 'is_local', True)),
        generated_at=timezone.localtime(), rounds=rounds,
    )
    for case in cases:
        result = ModelResult(question=case.question, note=case.note)
        for _i in range(max(1, rounds)):
            if case.prelude:
                _ask(user, case.prelude)
            run, latency, _policy = _ask(user, case.question)
            checks = _verdict(case, run, run.answer)
            tools = [s.get('tool') for s in (run.steps or []) if s.get('tool')]
            attempt = Attempt(
                status=run.status, tools=tools, answer=run.answer or '',
                tokens=run.total_tokens, steps=len(run.steps or []),
                latency=latency, error_code=run.error_code, checks=checks,
                ok=all(checks.values()),
            )
            result.attempts.append(attempt)
        result.ok = bool(result.attempts) and all(a.ok for a in result.attempts)
        if not result.ok:
            first_bad = next((a for a in result.attempts if not a.ok), result.attempts[0])
            result.failure = _failure_of(first_bad.checks, first_bad.tools)
        report.results.append(result)
    return report


def render_markdown(report, user=None):
    stamp = (report.generated_at or timezone.localtime()).strftime('%Y-%m-%d %H:%M')
    provider = f'{report.provider} / {report.model}' if report.provider else '（未配置模型）'
    lines = [
        '# AI Copilot 模型链路评估报告', '',
        f'- 生成时间：{stamp}',
        f'- 账号：{user.get_username() if user else "-"}',
        f'- 模型：**{provider}**（{"本地" if report.is_local else "云端"}）',
        f'- 用例：**{report.total}** 条，每题跑 **{report.rounds}** 次（两次都过才算过）',
        f'- 通过：**{report.passed}**，失败：**{report.total - report.passed}**',
        f'- 通过率：**{report.rate:.1f}%**，抖动率：**{report.flaky_rate:.0f}%**',
        f'- 平均工具步骤：**{report.avg("steps"):.1f}**，'
        f'平均 token：**{report.avg("tokens"):.0f}**，'
        f'平均耗时：**{report.avg("latency"):.1f}s**',
        '',
        '> 判定只看可机械验证的三件事：工具调对、查的是那个模块、答案里有要点且没有推脱话术。'
        '措辞质量需人工看明细。', '',
        '## 失败归类', '',
    ]
    counts = report.failure_counts()
    if counts:
        lines += ['| 归类 | 条数 |', '| --- | --- |']
        lines += [f'| {key} | {value} |' for key, value in counts.items()]
    else:
        lines.append('无失败项。')

    failed = [r for r in report.results if not r.ok]
    lines += ['', f'## 失败明细（{len(failed)} 条）', '']
    if failed:
        lines += ['| 问题 | 归类 | 工具 | 首答摘要 |', '| --- | --- | --- | --- |']
        for result in failed:
            bad = next((a for a in result.attempts if not a.ok), result.attempts[0])
            answer = (bad.answer or '').replace('\n', ' ')[:80].replace('|', '／')
            lines.append(f'| {result.question} | {result.failure} '
                         f'| {",".join(bad.tools) or "-"} | {answer or "（空）"} |')
    else:
        lines.append('无。')

    flaky = [r for r in report.results if len({a.ok for a in r.attempts}) > 1]
    if flaky:
        lines += ['', '## 抖动题（两次结果不一致）', '']
        lines += [f'- {r.question}' for r in flaky]
    return '\n'.join(lines) + '\n'
