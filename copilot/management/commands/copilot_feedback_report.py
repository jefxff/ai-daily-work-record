"""``manage.py copilot_feedback_report`` —— 反馈周报（不满意率 Top 10）。

指标口径刻意只做"错例来源"，不做满意度 KPI：内网助手一天可能只有十来次
问答，一条踩就能让满意率掉 8 个百分点，拿它考核会逼着大家不点踩。
这里只回答一个问题：**这周哪些问题答得不好，值不值得补本地链路**。

执行结果写 ``TaskRunLog``（定时任务页可见），方便每周一自动留痕。

用法：
    python manage.py copilot_feedback_report
    python manage.py copilot_feedback_report --days 30 --out docs/反馈周报.md
"""

from collections import Counter
from datetime import timedelta
from pathlib import Path

from django.core.management.base import BaseCommand
from django.utils import timezone

from copilot.models import CopilotFeedback, CopilotMessage


def _question_of(message):
    return (CopilotMessage.objects
            .filter(conversation_id=message.conversation_id,
                    role=CopilotMessage.Role.USER, id__lt=message.id)
            .order_by('-id').first())


def build_report(days=7):
    since = timezone.now() - timedelta(days=days)
    rows = list(CopilotFeedback.objects
                .filter(created_at__gte=since)
                .select_related('message')
                .order_by('-created_at'))
    bad = [fb for fb in rows if fb.vote == -1]
    good = [fb for fb in rows if fb.vote == 1]
    reasons = Counter(fb.reason or 'unspecified' for fb in bad)
    reason_labels = dict(CopilotFeedback.Reason.choices)
    reason_labels['unspecified'] = '未选择原因'

    questions = Counter()
    modules = Counter()
    for fb in bad:
        question = _question_of(fb.message)
        text = (question.content.strip() if question else '（未找到对应提问）')
        questions[text[:80]] += 1
        for ref in (fb.message.refs or []):
            if ref.get('module'):
                modules[ref['module']] += 1

    total = len(rows)
    rate = (len(bad) / total * 100) if total else 0.0
    lines = [
        '# AI 助手反馈周报', '',
        f'- 统计窗口：最近 **{days}** 天',
        f'- 生成时间：{timezone.localtime().strftime("%Y-%m-%d %H:%M")}',
        f'- 反馈总数：**{total}**（赞 {len(good)} / 踩 {len(bad)}）',
        f'- 不满意率：**{rate:.0f}%**',
        '',
        '> 该数字只作为"错例来源"，不用于满意度考核：样本太少，'
        '一条踩就能让比例大幅波动。', '',
        '## 踩的原因分布', '',
    ]
    if reasons:
        lines += ['| 原因 | 条数 |', '| --- | --- |']
        for code, count in reasons.most_common():
            lines.append(f'| {reason_labels.get(code, code)} | {count} |')
    else:
        lines.append('无。')

    lines += ['', f'## 被踩最多的问题（Top {min(10, len(questions))}）', '']
    if questions:
        lines += ['| 次数 | 问题 |', '| --- | --- |']
        for text, count in questions.most_common(10):
            lines.append(f'| {count} | {text.replace("|", "／")} |')
    else:
        lines.append('无。')

    if modules:
        lines += ['', '## 涉及的模块', '']
        lines += [f'- {key}：{count} 次' for key, count in modules.most_common()]

    lines += ['', '## 下一步', '',
              '把上表里的问题逐条确认后，用 `manage.py copilot_promote` 转成评估用例；'
              '能本地确定性回答的，补进 `planner` 对应意图而不是调提示词。']
    return '\n'.join(lines) + '\n', {'total': total, 'bad': len(bad), 'rate': rate}


class Command(BaseCommand):
    help = '输出 AI 助手反馈周报（不满意率与 Top 问题）'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=7, help='统计窗口天数（默认 7）')
        parser.add_argument('--out', default='', help='报告写入路径（Markdown）')
        parser.add_argument('--no-log', action='store_true', help='不写 TaskRunLog')

    def handle(self, *args, **options):
        started = timezone.now()
        text, stats = build_report(options['days'])
        self.stdout.write(text)

        if options['out']:
            path = Path(options['out'])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8')
            self.stdout.write(f'报告已写入 {path}')

        if not options['no_log']:
            self._log(started, stats)

    def _log(self, started, stats):
        """写执行历史：定时任务页能看到"这周反馈了多少、不满意率多少"。"""
        from notifications.models import ScheduledTask, TaskRunLog
        task, _ = ScheduledTask.objects.get_or_create(
            code='copilot_feedback_report',
            defaults={'name': 'AI 反馈周报', 'enabled': False,
                      'description': '统计 AI 助手点踩反馈，输出 Top 问题'},
        )
        now = timezone.now()
        TaskRunLog.objects.create(
            task=task, code=task.code, source=TaskRunLog.Source.MANUAL,
            status='success',
            duration_ms=int((now - started).total_seconds() * 1000),
            message=(f'反馈 {stats["total"]} 条，踩 {stats["bad"]} 条，'
                     f'不满意率 {stats["rate"]:.0f}%'),
            started_at=started, finished_at=now,
        )
