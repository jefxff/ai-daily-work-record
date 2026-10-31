"""``manage.py copilot_promote`` —— 把"点踩"的反馈转成评估用例草稿。

为什么要有这一步：``CopilotFeedback`` 只是"用户觉得答错了"的一句话，它本身
不构成回归——下周改完提示词，没人知道那条问题是否真的修好了。转成
``CopilotEvalCase`` 之后，它才会进 ``copilot_eval --with-model`` 的基线。

草稿 ``active=False``：问题、涉及模块、当时调用的工具自动带过来，但"标准答案
应该长什么样"必须人工判断——机器猜出来的期望值只会制造假绿。

用法：
    python manage.py copilot_promote --dry-run     # 只看会转哪几条
    python manage.py copilot_promote               # 真写草稿
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from copilot.models import (CopilotEvalCase, CopilotFeedback, CopilotMessage,
                            CopilotRun)


def _question_of(message):
    """取这条回答对应的提问：同一会话中它前面最近的一条 user 消息。"""
    return (CopilotMessage.objects
            .filter(conversation_id=message.conversation_id,
                    role=CopilotMessage.Role.USER, id__lt=message.id)
            .order_by('-id').first())


def _run_of(message, question):
    """取产生这条回答的 run（同会话 + 同问题，取最新一条）。"""
    if question is None:
        return None
    return (CopilotRun.objects
            .filter(conversation_id=message.conversation_id,
                    question=question.content)
            .order_by('-created_at').first())


def _tools_of(run):
    seen = []
    for step in (run.steps if run else []) or []:
        name = step.get('tool')
        if name and name not in seen:
            seen.append(name)
    return seen


def _scope_of(run, message=None):
    """用例涉及哪个模块：优先 run 的引用/步骤，其次看消息自带的引用。

    只看 run 会漏掉"run 已被清理、消息还在"的老反馈；消息上的 refs 是
    最后一次落库时写入的，同样可靠。
    """
    sources = [getattr(run, 'refs', None) or [],
               getattr(run, 'steps', None) or [],
               getattr(message, 'refs', None) or []]
    for items in sources:
        for item in items:
            if isinstance(item, dict) and item.get('module'):
                return item['module']
    return ''


class Command(BaseCommand):
    help = '把最近点踩的反馈转成评估用例草稿（active=False，需人工确认）'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=7, help='回看天数（默认 7）')
        parser.add_argument('--limit', type=int, default=50, help='最多转换条数（默认 50）')
        parser.add_argument('--dry-run', action='store_true', help='只打印，不写库')

    def handle(self, *args, **options):
        since = timezone.now() - timedelta(days=options['days'])
        rows = (CopilotFeedback.objects
                .filter(vote=-1, created_at__gte=since, eval_cases__isnull=True)
                .select_related('message')
                .order_by('-created_at')[:options['limit']])

        made, skipped = [], 0
        for fb in rows:
            question = _question_of(fb.message)
            if question is None or not question.content.strip():
                skipped += 1
                continue
            run = _run_of(fb.message, question)
            case = CopilotEvalCase(
                question=question.content.strip(),
                expected_tools=_tools_of(run),
                module_scope=_scope_of(run, fb.message),
                expected_keywords=[],
                must_not_contain=[],
                mode=CopilotEvalCase.Mode.MODEL,
                active=False,
                source_feedback=fb,
            )
            made.append(case)

        if options['dry_run']:
            self.stdout.write(f'[dry-run] 将生成 {len(made)} 条草稿'
                              f'（跳过 {skipped} 条无法定位提问的反馈）')
            for case in made:
                self.stdout.write(
                    f'  · {case.question[:60]} | 模块 {case.module_scope or "-"}'
                    f' | 工具 {",".join(case.expected_tools) or "-"}')
            return

        with transaction.atomic():
            CopilotEvalCase.objects.bulk_create(made)
        self.stdout.write(f'已生成评估用例草稿 {len(made)} 条（跳过 {skipped} 条）')
        self.stdout.write('下一步：在 Django admin 的「AI 评估用例」里补齐答案要点后勾选启用。')
