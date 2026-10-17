from django.apps import AppConfig


class SysbackupConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "sysbackup"
    verbose_name = "数据备份"
