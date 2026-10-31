"""管理命令：回收卡住的 AI 问答任务。

    python manage.py copilot_reap              # 只回收心跳超时的
    python manage.py copilot_reap --interrupt  # 重启后清理遗留的 running/pending
"""

from django.core.management.base import BaseCommand

from copilot import tasks


class Command(BaseCommand):
    help = '回收超时或遗留的 AI 问答任务'

    def add_arguments(self, parser):
        parser.add_argument('--interrupt', action='store_true',
                            help='把所有未完成的 run 标记为 interrupted（服务重启后使用）')

    def handle(self, *args, **options):
        count = tasks.reap_stale_runs()
        self.stdout.write(f'已回收超时任务 {count} 个')
        if options['interrupt']:
            interrupted = tasks.interrupt_unfinished()
            self.stdout.write(f'已中断遗留任务 {interrupted} 个')
