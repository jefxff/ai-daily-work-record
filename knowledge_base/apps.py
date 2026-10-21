from django.apps import AppConfig


class KnowledgeBaseConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'knowledge_base'
    verbose_name = '知识库'

    def ready(self):
        # 每日记录删除 → 清理知识库里的沉淀节点（影子跟着本体走）。
        # 注册在本 app 而不是 records（records 尚无 AppConfig），避免动既有配置。
        from django.db.models.signals import post_delete

        from records.kb_sync import cleanup_kb_on_record_delete
        from records.models import DailyRecord

        post_delete.connect(cleanup_kb_on_record_delete, sender=DailyRecord,
                            dispatch_uid='kb_sync_on_daily_record_delete')
