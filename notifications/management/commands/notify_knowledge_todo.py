"""知识待办到期提醒扫描（幕布化批次 5）。

扫描大纲待办节点（node_type=todo, is_done=False, 有 due_date）：
- 窗口：[今天 - overdue_days, 今天 + days]，即临期与逾期都提醒；
- 接收人：文档 owner 本人（他人私有文档里的待办与任何人都无关，
  owner 恒可见自己的文档，无需再做可见性过滤）；
- 链接直达编辑器并定位节点：/knowledge/document/<pk>/outline/?node=<uid>；
- 幂等：dedup_context 含节点主键与日期，同日重跑不重复发送。

用法（经 notifications 进程内调度器按 ScheduledTask 配置执行，也可手动）：
    python manage.py notify_knowledge_todo
    python manage.py notify_knowledge_todo --days 3 --overdue_days 7
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from notifications import services


class Command(BaseCommand):
    help = "知识待办到期提醒：扫描大纲待办节点，提醒文档 owner"

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=3,
                            help="提前提醒天数（默认 3）")
        parser.add_argument("--overdue_days", type=int, default=7,
                            help="逾期回溯天数（默认 7，超过则不再提醒）")

    def handle(self, *args, **opts):
        from knowledge_base.models import KnowledgeNode

        today = timezone.localdate()
        start = today - timedelta(days=opts["overdue_days"])
        deadline = today + timedelta(days=opts["days"])

        created = 0
        targets = (KnowledgeNode.objects
                   .filter(node_type="todo", is_done=False,
                           due_date__isnull=False,
                           due_date__gte=start, due_date__lte=deadline)
                   .exclude(document__owner__isnull=True)
                   .select_related("document"))
        for node in targets:
            doc = node.document
            days_left = (node.due_date - today).days
            if days_left > 0:
                days_left_word = f"还有 {days_left} 天到期"
            elif days_left == 0:
                days_left_word = "今天到期"
            else:
                days_left_word = f"已逾期 {-days_left} 天"
            if services.emit(
                "knowledge_todo_due",
                recipient=doc.owner,
                context={
                    "doc_title": doc.title,
                    "todo_text": node.text[:100],
                    "due_date": node.due_date,
                    "days_left": days_left,
                    "days_left_word": days_left_word,
                },
                dedup_context=f"kb_todo:{node.pk}:{today}",
                link=f"/knowledge/document/{doc.pk}/outline/?node={node.uid}",
            ):
                created += 1

        self.stdout.write(f"知识待办到期提醒: 新建 {created} 条")
