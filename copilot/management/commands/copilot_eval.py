"""``manage.py copilot_eval`` —— 跑全模块问句矩阵，输出 Markdown 报告。

用法：
    python manage.py copilot_eval                    # 打印到屏幕
    python manage.py copilot_eval --out docs/评估.md  # 同时写文件
    python manage.py copilot_eval --user admin       # 指定按哪个账号的权限跑
    python manage.py copilot_eval --fail-on-error     # 有失败项时以非 0 退出（CI 用）

默认账号是第一个超级管理员；换成普通账号可以看到"权限裁剪后的模块范围"。
"""

from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from copilot import eval_matrix, security


class Command(BaseCommand):
    help = '跑 AI Copilot 全模块问句评估，输出 Markdown 报告'

    def add_arguments(self, parser):
        parser.add_argument('--user', default='', help='按哪个账号的权限评估（默认第一个超级管理员）')
        parser.add_argument('--out', default='', help='报告写入路径（Markdown）')
        parser.add_argument('--fail-on-error', action='store_true',
                            help='有失败用例时以退出码 1 结束')

    def handle(self, *args, **options):
        User = get_user_model()
        username = options['user']
        if username:
            user = User.objects.filter(username=username).first()
            if user is None:
                raise CommandError(f'账号不存在：{username}')
        else:
            user = User.objects.filter(is_superuser=True).order_by('id').first()
            if user is None:
                raise CommandError('没有超级管理员账号，请用 --user 指定')

        report = eval_matrix.run_all(user, security.policy())
        text = eval_matrix.render_markdown(report, user=user)
        self.stdout.write(text)

        out = options['out']
        if out:
            path = Path(out)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8')
            self.stdout.write(f'报告已写入 {path}')

        if options['fail_on_error'] and report.failed:
            raise CommandError(f'{report.failed} 条用例失败')
