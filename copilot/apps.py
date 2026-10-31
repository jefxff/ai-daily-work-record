from django.apps import AppConfig


class CopilotConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'copilot'
    verbose_name = 'AI 助手'

    def ready(self):
        # 工具注册：import 即向 registry 注册，视图/引擎据此构建工具清单。
        # 注意这里只做内存注册，不碰数据库（迁移期间也会执行 ready）。
        from . import tools  # noqa: F401
