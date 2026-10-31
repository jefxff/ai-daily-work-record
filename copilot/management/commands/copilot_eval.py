"""``manage.py copilot_eval`` —— 跑全模块问句矩阵，输出 Markdown 报告。

用法：
    python manage.py copilot_eval                    # 打印到屏幕
    python manage.py copilot_eval --out docs/评估.md  # 同时写文件
    python manage.py copilot_eval --user admin       # 指定按哪个账号的权限跑
    python manage.py copilot_eval --fail-on-error     # 有失败项时以非 0 退出（CI 用）

    python manage.py copilot_eval --seed-model-cases  # 把内置金样集写进表
    python manage.py copilot_eval --with-model        # 真实调用模型跑金样集
    python manage.py copilot_eval --with-model --model-limit 5 --rounds 1   # 冒烟

默认账号是第一个超级管理员；换成普通账号可以看到"权限裁剪后的模块范围"。
"""

from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from copilot import eval_matrix, model_eval, security
from copilot.model_cases import MODEL_CASES
from copilot.models import CopilotEvalCase


class Command(BaseCommand):
    help = '跑 AI Copilot 全模块问句评估，输出 Markdown 报告'

    def add_arguments(self, parser):
        parser.add_argument('--user', default='', help='按哪个账号的权限评估（默认第一个超级管理员）')
        parser.add_argument('--out', default='', help='报告写入路径（Markdown）')
        parser.add_argument('--fail-on-error', action='store_true',
                            help='有失败用例时以退出码 1 结束')
        parser.add_argument('--with-model', action='store_true',
                            help='同时跑模型链路金样集（会真实调用模型、消耗 token）')
        parser.add_argument('--model-limit', type=int, default=0,
                            help='模型用例只跑前 N 条（冒烟用）')
        parser.add_argument('--rounds', type=int, default=2,
                            help='模型用例每题跑几次（默认 2，两次都过才算过）')
        parser.add_argument('--model-out', default='',
                            help='模型链路报告写入路径（默认 docs/eval/ 下按日期命名）')
        parser.add_argument('--seed-model-cases', action='store_true',
                            help='把内置金样集写入 CopilotEvalCase（已存在则跳过）')
        parser.add_argument('--no-local', action='store_true',
                            help='跳过本地矩阵，只跑模型链路')

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

        if options['seed_model_cases']:
            created = self._seed()
            self.stdout.write(f'金样集写入完成：新增 {created} 条'
                              f'（内置共 {len(MODEL_CASES)} 条）')

        report = None
        if not options['no_local']:
            report = eval_matrix.run_all(user, security.policy())
            text = eval_matrix.render_markdown(report, user=user)
            self.stdout.write(text)

            out = options['out']
            if out:
                path = Path(out)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding='utf-8')
                self.stdout.write(f'报告已写入 {path}')

        model_report = None
        if options['with_model']:
            model_report = self._run_model(user, options)

        if options['fail_on_error']:
            failed = report.failed if report else 0
            if model_report is not None:
                failed += model_report.total - model_report.passed
            if failed:
                raise CommandError(f'{failed} 条用例失败')

    def _seed(self):
        created = 0
        for case in MODEL_CASES:
            _obj, made = CopilotEvalCase.objects.get_or_create(
                question=case.question,
                defaults={
                    'expected_tools': list(case.tools),
                    'expected_keywords': list(case.keywords),
                    'must_not_contain': list(case.forbid),
                    'module_scope': case.scope,
                    'prelude': case.prelude,
                    'mode': CopilotEvalCase.Mode.MODEL,
                    'active': True,
                },
            )
            created += 1 if made else 0
        return created

    def _run_model(self, user, options):
        self.stdout.write(self.style.WARNING(
            '开始模型链路评估：会真实调用模型并消耗 token，请耐心等待…'))
        report = model_eval.run_cases(
            user, cases=MODEL_CASES,
            rounds=options['rounds'], limit=options['model_limit'])
        text = model_eval.render_markdown(report, user=user)
        self.stdout.write(text)

        out = options['model_out']
        if not out:
            stamp = report.generated_at.strftime('%Y%m%d')
            out = f'docs/eval/模型链路基线_{stamp}.md'
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        self.stdout.write(f'模型链路报告已写入 {path}')
        return report
