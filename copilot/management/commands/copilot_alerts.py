"""按确定性规则生成主动提醒（B3-2）。

用法::

    python manage.py copilot_alerts              # 打印命中情况，不发通知（默认）
    python manage.py copilot_alerts --confirm    # 真正推送
    python manage.py copilot_alerts --mute complaint_surge
    python manage.py copilot_alerts --unmute complaint_surge

为什么默认干跑：提醒一旦误报会直接消耗用户对**所有**推送的注意力，
第一次跑必须让人先看一眼命中内容再决定要不要推。
"""
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from notifications.services import emit, users_with_perm

from copilot import rules


class Command(BaseCommand):
    help = '按确定性规则扫描业务异常并推送提醒'

    def add_arguments(self, parser):
        parser.add_argument('--username', help='只为指定账号扫描（调试用）')
        parser.add_argument('--confirm', action='store_true',
                            help='推送通知（默认只打印）')
        parser.add_argument('--mute', metavar='RULE',
                            help='屏蔽某条规则（写进定时任务参数，长期生效）')
        parser.add_argument('--unmute', metavar='RULE', help='恢复某条规则')

    def handle(self, *args, **options):
        if options['mute'] or options['unmute']:
            code = options['mute'] or options['unmute']
            try:
                current = rules.set_muted(code, muted=bool(options['mute']))
            except KeyError:
                raise CommandError(
                    f'未知规则：{code}（可用：{", ".join(rules.RULES)}）')
            action = '已屏蔽' if options['mute'] else '已恢复'
            self.stdout.write(self.style.SUCCESS(
                f'{action} {code}；当前屏蔽：{", ".join(current) or "（无）"}'))
            return

        today = timezone.localdate()
        muted = rules.muted_rules()
        if muted:
            self.stdout.write(f'已屏蔽规则：{", ".join(sorted(muted))}')

        if options['username']:
            from django.contrib.auth import get_user_model
            targets = list(get_user_model().objects.filter(
                username=options['username'], is_active=True))
            if not targets:
                raise CommandError(f"账号不存在：{options['username']}")
        else:
            targets = list(users_with_perm('copilot', 'read'))

        hit_users = pushed = total_hits = 0
        is_dry = not options['confirm']
        for user in targets:
            if not user.is_active:
                continue
            alerts = rules.evaluate(user, today=today, skip=muted)
            if not alerts:
                continue
            hit_users += 1
            total_hits += len(alerts)
            self.stdout.write(f'[{user.get_username()}]')
            for alert in alerts:
                self.stdout.write(f'  · {alert["title"]}（{alert["rule"]}）')
                if is_dry:
                    continue
                sent = emit(
                    'copilot_alert',
                    recipient=user,
                    context={'title': alert['title'], 'body': alert['body'],
                             'rule': alert['rule']},
                    # 同一规则同一天对每个用户只提醒一次
                    dedup_context=f'alert:{alert["rule"]}:{user.pk}:{today.isoformat()}',
                    link=alert.get('link') or '',
                )
                pushed += 1 if sent is not None else 0

        tail = '（dry-run，未推送）' if is_dry else ''
        self.stdout.write(self.style.SUCCESS(
            f'规则扫描完成{tail}：{len(targets)} 个账号，命中 {hit_users} 人 / '
            f'{total_hits} 条，推送 {pushed} 条'))
