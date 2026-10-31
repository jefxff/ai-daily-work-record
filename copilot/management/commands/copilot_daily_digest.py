"""生成并推送每日业务简报。

用法::

    python manage.py copilot_daily_digest               # 干热锅：打印不建会话
    python manage.py copilot_daily_digest --dry-run     # 同上（默认）
    python manage.py copilot_daily_digest --confirm     # 写会话并发通知
    python manage.py copilot_daily_digest --username alice

默认Dry-run：这是个写库 + 打扰人的动作，宁可让第一次执行的人多敲一次
``--confirm``，也不要"Cron 配错了就把 50 个人天天刷一遍"。
"""
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from notifications.services import emit, users_with_perm
from copilot import digest as digest_mod


class Command(BaseCommand):
    help = '生成每日业务简报（到期/客诉/日报/借阅），写为会话并推送站内通知'

    def add_arguments(self, parser):
        parser.add_argument('--date', help='指定日期（YYYY-MM-DD，默认今天）')
        parser.add_argument('--username', help='只为指定账号生成（调试用）')
        parser.add_argument('--dry-run', '--dry', action='store_true',
                            help='只打印，不写会话、不发通知')
        parser.add_argument('--confirm', action='store_true',
                            help='真正执行写入与推送')
        parser.add_argument('--notify', action='store_true',
                            help='推送站内通知（默认随 --confirm 一起生效）')
        parser.add_argument('--force', action='store_true',
                            help='周末也生成（默认跳过周六周日）')

    def handle(self, *args, **options):
        today = timezone.localdate()
        if options['date']:
            try:
                today = timezone.datetime.strptime(
                    options['date'], '%Y-%m-%d').date()
            except ValueError:
                raise CommandError('--date 需要形如 2026-10-31')

        dry = options['dry_run'] or not options['confirm']
        do_notify = options['notify'] or (not dry and not options['dry_run'])

        # 周末跳过：任务是"每天定时"，但没有 scheduled 的星期语义。
        # 周六早上发一份"今天要处理的到期事项"，只会让人周一才干时找不到。
        if today.weekday() >= 5 and not options['force'] and not options['username']:
            self.stdout.write(
                f'{today:%Y-%m-%d} 是周末，简报已跳过（加 --force 可强制生成）')
            return

        if options['username']:
            from django.contrib.auth import get_user_model
            targets = list(get_user_model().objects.filter(
                username=options['username'], is_active=True))
            if not targets:
                raise CommandError(f"账号不存在：{options['username']}")
        else:
            targets = list(users_with_perm('copilot', 'read'))

        written = notified = 0
        for user in targets:
            if not user.is_active:
                continue
            markdown, sections = digest_mod.build(user, today=today)
            headline = digest_mod.headline(sections)
            self.stdout.write(f'[{user.get_username()}] {headline}')
            if dry:
                if self.verbosity(options) >= 2:
                    self.stdout.write(markdown)
                continue

            conversation = digest_mod.save_conversation(user, markdown, today=today)
            written += 1
            if do_notify and self._notify(user, headline, conversation):
                notified += 1

        scope = '（dry-run，未写入）' if dry else ''
        self.stdout.write(self.style.SUCCESS(
            f'简报生成完成{scope}：{len(targets)} 个账号，写会话 {written} 份，'
            f'推送通知 {notified} 条'))

    def verbosity(self, options):
        return int(options.get('verbosity') or 1)

    def _notify(self, user, headline, conversation):
        """发站内通知；同一天同一人只发一次（幂等键）。

        补跑定时任务、手动执行 duang 一遍都不该让人收到两份同样的简报。
        """
        sent = emit(
            'copilot_daily_digest',
            recipient=user,
            context={'headline': headline, 'date': timezone.localdate().isoformat()},
            dedup_context=f'digest:{user.pk}:{timezone.localdate().isoformat()}',
            link=f'/copilot/c/{conversation.pk}/',
        )
        return sent is not None
