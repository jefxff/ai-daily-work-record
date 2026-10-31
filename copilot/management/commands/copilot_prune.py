"""``manage.py copilot_prune`` —— 执行会话保留策略 + 外发日志保留。

策略表里写着"会话保留 90 天"，但在此之前没有任何任务真的删过东西：等于
对合规的承诺只落在配置界面上。这个命令是那句承诺的执行体。

三条口径（都可配）：
1. 活跃会话超过 ``CopilotPolicy.retention_days`` → 连带消息/run/工具调用一起删；
2. 已归档会话超过 30 天 → 物理删除（用户点过删除，留 30 天反悔期）；
3. 外发日志超过 ``egress_retention_days``（默认 180 天）→ 删；**不随会话删**，
   否则"谁把什么发给了哪家模型"会跟着会话一起消失。

默认 dry-run：先看数，再 ``--confirm``。误删不可逆，宁可多敲一个参数。

用法：
    python manage.py copilot_prune                 # 只统计
    python manage.py copilot_prune --confirm       # 真删
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from copilot.models import (CopilotConversation, CopilotEgressLog, CopilotMessage,
                            CopilotPolicy, CopilotRun, CopilotToolCall)

ARCHIVED_GRACE_DAYS = 30


class Command(BaseCommand):
    help = '按策略清理过期 AI 会话、工具调用与外发日志（默认 dry-run）'

    def add_arguments(self, parser):
        parser.add_argument('--confirm', action='store_true',
                            help='真正执行删除（不加则只统计）')
        parser.add_argument('--days', type=int, default=0,
                            help='覆盖策略里的会话保留天数（默认取 CopilotPolicy）')
        parser.add_argument('--egress-days', type=int, default=0,
                            help='覆盖外发日志保留天数（默认取 CopilotPolicy）')

    def handle(self, *args, **options):
        policy = CopilotPolicy.current()
        retention = options['days'] or max(1, policy.retention_days)
        egress_days = options['egress_days'] or max(1, policy.egress_retention_days)
        now = timezone.now()
        session_cutoff = now - timedelta(days=retention)
        archive_cutoff = now - timedelta(days=ARCHIVED_GRACE_DAYS)
        egress_cutoff = now - timedelta(days=egress_days)

        expired = CopilotConversation.objects.filter(
            status=CopilotConversation.Status.ACTIVE,
            last_active_at__lt=session_cutoff,
        )
        archived = CopilotConversation.objects.filter(
            status=CopilotConversation.Status.ARCHIVED,
            archived_at__lt=archive_cutoff,
        )
        doomed = CopilotConversation.objects.filter(
            pk__in=list(expired.values_list('pk', flat=True))
            + list(archived.values_list('pk', flat=True)))
        egress = CopilotEgressLog.objects.filter(created_at__lt=egress_cutoff)
        # 进程被强杀时留下的 running/pending：不会有人再推进它们，
        # 但也不要因为"保留期没到"就一直挂着占用唯一约束。
        stranded = CopilotRun.objects.filter(
            status__in=[CopilotRun.Status.PENDING, CopilotRun.Status.RUNNING],
            created_at__lt=session_cutoff,
        )

        stats = {
            'sessions_expired': expired.count(),
            'sessions_archived': archived.count(),
            'messages': CopilotMessage.objects.filter(conversation__in=doomed).count(),
            'runs': CopilotRun.objects.filter(conversation__in=doomed).count(),
            'tool_calls': CopilotToolCall.objects.filter(conversation__in=doomed).count(),
            'egress': egress.count(),
            'stranded': stranded.count(),
        }

        mode = '执行' if options['confirm'] else 'dry-run'
        self.stdout.write(
            f'[{mode}] 会话保留 {retention} 天 / 外发日志保留 {egress_days} 天')
        self.stdout.write(
            f'  过期会话 {stats["sessions_expired"]} 个，'
            f'归档超期 {stats["sessions_archived"]} 个'
            f'（将连带删除消息 {stats["messages"]} 条、run {stats["runs"]} 个、'
            f'工具调用 {stats["tool_calls"]} 条）')
        self.stdout.write(f'  过期外发日志 {stats["egress"]} 条')
        self.stdout.write(f'  遗留未完成 run {stats["stranded"]} 个（标记为 interrupted）')

        if not options['confirm']:
            self.stdout.write('未做任何修改。确认无误后加 --confirm 执行。')
            return

        with transaction.atomic():
            if stats['stranded']:
                stranded.update(status=CopilotRun.Status.INTERRUPTED,
                                finished_at=now, error_code='INTERRUPTED',
                                answer='服务重启导致本次问答中断，请重新提问')
            deleted_sessions = doomed.delete()[0] if doomed.exists() else 0
            deleted_egress = egress.delete()[0] if egress.exists() else 0
        self.stdout.write(f'完成：删除记录 {deleted_sessions} 行（含级联），'
                          f'外发日志 {deleted_egress} 行')
