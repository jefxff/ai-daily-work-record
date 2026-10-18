"""notifications 的改造验收测试（对应技术审计报告 v4 的 #11）。

核心断言：
- 调度逻辑与"谁来触发"解耦——run_due_tasks 可被进程内线程和独立进程共用；
- should_stop 能在收到退出信号时中断本轮，不需要等所有任务跑完；
- 进程内调度由显式开关（NOTIFICATIONS_SCHEDULER_INPROCESS）控制，
  且测试进程里绝不会被常驻线程污染。
"""

import threading
from io import StringIO

from django.conf import settings
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from . import scheduler
from .models import ScheduledTask


class RunDueTasksTests(TestCase):
    def setUp(self):
        # 种子数据里的任务可能被启用，统一关掉，保证断言只针对本用例创建的任务
        ScheduledTask.objects.update(enabled=False)
        # 该 code 由迁移 0004 种子数据预置，用 update_or_create 避免唯一键冲突
        self.task, _ = ScheduledTask.objects.update_or_create(
            code="check_daily_incomplete",
            defaults={
                "name": "每日记录未完成提醒",
                "enabled": True,
                "trigger_type": ScheduledTask.TriggerType.INTERVAL,
                "interval_minutes": 60,
                "last_run_at": None,
                "last_status": "idle",
                "params": {"skip_history": True},
            },
        )

    def test_due_task_is_executed_once(self):
        ran = scheduler.run_due_tasks()
        self.assertEqual(ran, 1)
        self.task.refresh_from_db()
        self.assertIn(self.task.last_status, ("success", "fail"))
        self.assertIsNotNone(self.task.last_run_at)
        # 刚跑过，间隔未到 → 第二轮不应重复执行
        self.assertEqual(scheduler.run_due_tasks(), 0)

    def test_should_stop_breaks_the_round(self):
        """收到退出信号时本轮立即中断，不执行任何任务。"""
        ran = scheduler.run_due_tasks(should_stop=lambda: True)
        self.assertEqual(ran, 0)
        self.task.refresh_from_db()
        self.assertIsNone(self.task.last_run_at)

    def test_disabled_task_is_skipped(self):
        ScheduledTask.objects.filter(pk=self.task.pk).update(enabled=False)
        self.assertEqual(scheduler.run_due_tasks(), 0)


class RunSchedulerCommandTests(TestCase):
    def setUp(self):
        ScheduledTask.objects.update(enabled=False)
        # 该 code 由迁移 0004 种子数据预置，用 update_or_create 避免唯一键冲突
        self.task, _ = ScheduledTask.objects.update_or_create(
            code="check_daily_incomplete",
            defaults={
                "name": "每日记录未完成提醒",
                "enabled": True,
                "trigger_type": ScheduledTask.TriggerType.INTERVAL,
                "interval_minutes": 60,
                "last_run_at": None,
                "last_status": "idle",
                "params": {"skip_history": True},
            },
        )

    def test_once_flag_runs_and_exits(self):
        """--once 是排障入口：跑一轮就退出，不会常驻。"""
        out = StringIO()
        call_command("run_scheduler", "--once", "--no-worker", stdout=out)
        self.assertIn("已执行 1 个到期任务", out.getvalue())
        self.task.refresh_from_db()
        self.assertIsNotNone(self.task.last_run_at)

    @override_settings(NOTIFICATIONS_SCHEDULER_INPROCESS=True)
    def test_warns_when_inprocess_scheduler_is_on(self):
        """进程内已有一份调度线程时必须告警，否则定时任务会被执行两次。"""
        out = StringIO()
        call_command("run_scheduler", "--once", "--no-worker", stdout=out)
        self.assertIn("NOTIFICATIONS_SCHEDULER_INPROCESS", out.getvalue())


class InProcessSchedulerTests(TestCase):
    def test_flag_exists_in_settings(self):
        self.assertTrue(hasattr(settings, "NOTIFICATIONS_SCHEDULER_INPROCESS"))

    def test_no_background_thread_in_test_process(self):
        """manage.py test 下不能起常驻线程，否则测试之间互相污染。"""
        names = [t.name for t in threading.enumerate()]
        self.assertNotIn("notify-scheduler", names)
        self.assertNotIn("notify-worker", names)

    def test_heartbeats_are_keyed(self):
        """心跳键供通知中心页面展示"调度器是否活着"。"""
        self.assertIn("scheduler", scheduler.HEARTBEATS)
        self.assertIn("worker", scheduler.HEARTBEATS)
        self.assertIsInstance(scheduler.worker_alive(), bool)

    def test_next_run_at_for_interval_task(self):
        task = ScheduledTask(
            enabled=True,
            trigger_type=ScheduledTask.TriggerType.INTERVAL,
            interval_minutes=30,
            last_run_at=timezone.now(),
        )
        nxt = scheduler.next_run_at(task)
        self.assertIsNotNone(nxt)
        self.assertGreater(nxt, timezone.now())
