"""A4 定时任务连续失败告警测试。

覆盖：
- 同一任务连续失败达到阈值（3 次）→ 产生 scheduled_task_failure 通知，
  且收件人恰为具备 system.update 权限的用户；
- 成功执行会清零连续失败计数（再失败一次不触发告警）。

直接调用 scheduler._handle_task_result（可单测入口）；用例间清理进程内计数。

运行：python manage.py test notifications.tests_task_failure -v 1
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from notifications import scheduler, services
from notifications.models import Notification, ScheduledTask

User = get_user_model()


def grant_perms(user, *codes):
    """为测试用户授予 RBAC 权限码（沿用 tests.py 既有写法）。"""
    from accounts.models import Permission, Role

    role = Role.objects.create(code=f"test-role-{user.username}", name="测试角色")
    role.permissions.set(Permission.objects.filter(code__in=codes))
    user.rbac_roles.add(role)


class TaskFailureAlertTests(TestCase):
    def setUp(self):
        # 清理模块级进程内计数，避免用例间串扰
        scheduler._FAIL_STREAK.clear()
        self.task = ScheduledTask.objects.create(
            code="test_task_fail", name="测试失败任务", enabled=True,
        )
        # 具备 system.update 权限的管理员（预期收件人）
        self.admin = User.objects.create_user("fail_admin", password="x12345678")
        grant_perms(self.admin, "system.update")
        # 无该权限的普通用户（不应收到）
        self.staff = User.objects.create_user("fail_staff", password="x12345678")

    def _failure_notifications(self):
        return Notification.objects.filter(template__code="scheduled_task_failure")

    def test_three_consecutive_failures_alert_recipients(self):
        """连续失败 3 次触发告警，收件人恰为 system.update 权限用户。"""
        recipients = list(services.users_with_perm("system", "update"))
        recipient_pks = {u.pk for u in recipients}
        self.assertIn(self.admin.pk, recipient_pks)
        self.assertNotIn(self.staff.pk, recipient_pks)

        for _ in range(3):
            scheduler._handle_task_result(self.task, ok=False, message="boom")

        qs = self._failure_notifications()
        self.assertEqual(qs.count(), len(recipients))
        self.assertEqual(set(qs.values_list("recipient_id", flat=True)), recipient_pks)
        self.assertNotIn(self.staff.pk, set(qs.values_list("recipient_id", flat=True)))
        # 计数与上下文正确
        self.assertEqual(scheduler._FAIL_STREAK.get(self.task.pk), 3)
        out = qs.first()
        self.assertIn("3", out.content)
        self.assertIn("boom", out.content)
        self.assertEqual(out.link, "/system/health/")
        # 同日重复失败不再新增（dedup 幂等）
        scheduler._handle_task_result(self.task, ok=False, message="boom2")
        self.assertEqual(self._failure_notifications().count(), len(recipients))

    def test_success_resets_streak(self):
        """成功一次后连续失败计数清零，随后再失败一次不触发告警。"""
        scheduler._handle_task_result(self.task, ok=False, message="a")
        scheduler._handle_task_result(self.task, ok=False, message="b")
        self.assertEqual(scheduler._FAIL_STREAK.get(self.task.pk), 2)

        scheduler._handle_task_result(self.task, ok=True, message="ok")
        self.assertNotIn(self.task.pk, scheduler._FAIL_STREAK)

        scheduler._handle_task_result(self.task, ok=False, message="c")
        self.assertEqual(self._failure_notifications().count(), 0)
        self.assertEqual(scheduler._FAIL_STREAK.get(self.task.pk), 1)

    def test_below_threshold_no_alert(self):
        """未达阈值（失败 2 次）不产生告警。"""
        scheduler._handle_task_result(self.task, ok=False, message="x")
        scheduler._handle_task_result(self.task, ok=False, message="y")
        self.assertEqual(self._failure_notifications().count(), 0)
