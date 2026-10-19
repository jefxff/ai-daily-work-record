"""通知模块测试：模型约束 / emit 幂等聚合 / 渠道派发 / worker 生命周期 / 用户隔离 / RBAC。

运行：python manage.py test notifications -v 2
"""
from datetime import datetime, time as dtime, timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from notifications import services
from notifications.models import (
    Notification,
    NotificationDelivery,
    NotificationPreference,
    NotificationTemplate,
    ScheduledTask,
)

User = get_user_model()


def grant_perms(user, *codes):
    """为测试用户授予 RBAC 权限码。

    测试库中 Permission 行由 accounts.0002_init_rbac 迁移按当前 registry 种子，
    故 notifications.read / insurance.read 等码可直接按 code 过滤授权。
    """
    from accounts.models import Permission, Role

    role = Role.objects.create(code=f"test-role-{user.username}", name="测试角色")
    role.permissions.set(Permission.objects.filter(code__in=codes))
    user.rbac_roles.add(role)


def make_template(**overrides):
    defaults = dict(
        code="test_tpl",
        name="测试模板",
        title_template="提醒：{{ thing }}",
        content_template="内容 {{ thing }}（{{ date|date:'Y-m-d' }}）",
        email_subject_template="邮件 {{ thing }}",
        email_body_template="邮件正文 {{ thing }}",
        category="task",
        is_active=True,
    )
    defaults.update(overrides)
    return NotificationTemplate.objects.create(**defaults)


class BaseModelTests(TestCase):
    def test_dedup_key_unique(self):
        """幂等键唯一约束：同键第二条落库被数据库拒绝。"""
        user = User.objects.create_user("u1", password="x12345678")
        tpl = make_template()
        Notification.objects.create(recipient=user, template=tpl, title="t",
                                    content="c", dedup_key="dup-key")
        with self.assertRaises(Exception):
            Notification.objects.create(recipient=user, template=tpl, title="t",
                                        content="c", dedup_key="dup-key")

    def test_str_and_badge(self):
        user = User.objects.create_user("u1", password="x12345678")
        tpl = make_template()
        n = Notification.objects.create(recipient=user, template=tpl, title="t", content="c")
        self.assertIn("t", str(n))
        self.assertEqual(n.status_badge_class, "info")
        self.assertTrue(n.can_revoke)  # pending 可撤回


class EmitTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("u1", password="x12345678")
        self.tpl = make_template()

    def test_emit_creates_pending_with_snapshot(self):
        """emit 落库为 pending，渲染快照（模板后续修改不影响已发通知）。"""
        n = services.emit("test_tpl", recipient=self.user, context={"thing": "周报", "date": timezone.now()})
        self.assertIsNotNone(n)
        self.assertEqual(n.status, Notification.Status.PENDING)
        self.assertEqual(n.title, "提醒：周报")
        self.assertIn("周报", n.content)
        self.assertEqual(n.category, "task")
        # 模板改了不影响已落库通知
        self.tpl.title_template = "改过了 {{ thing }}"
        self.tpl.save()
        n.refresh_from_db()
        self.assertEqual(n.title, "提醒：周报")

    def test_emit_dedup_idempotent(self):
        """同 dedup_context 重复触发只建一条，返回已有通知。"""
        ctx = {"thing": "a", "date": timezone.now()}
        first = services.emit("test_tpl", recipient=self.user, context=ctx,
                              dedup_context="record:1")
        second = services.emit("test_tpl", recipient=self.user, context=ctx,
                               dedup_context="record:1")
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Notification.objects.count(), 1)

    def test_emit_agg_merges_in_window(self):
        """聚合降噪：偏好窗口内同 agg_key 触发合并计数，不新建行。"""
        ctx = {"thing": "a", "date": timezone.now()}
        first = services.emit("test_tpl", recipient=self.user, context=ctx,
                              agg_context="thing:a")
        second = services.emit("test_tpl", recipient=self.user, context=ctx,
                               agg_context="thing:a")
        self.assertEqual(first.pk, second.pk)
        second.refresh_from_db()
        self.assertEqual(second.agg_count, 2)
        self.assertEqual(Notification.objects.count(), 1)

    def test_emit_agg_disabled_by_pref(self):
        """用户偏好聚合窗口=0 时不合并。"""
        services.get_prefs(self.user).agg_window_minutes = 0
        NotificationPreference.objects.filter(user=self.user).update(agg_window_minutes=0)
        ctx = {"thing": "a", "date": timezone.now()}
        a = services.emit("test_tpl", recipient=self.user, context=ctx, agg_context="k")
        b = services.emit("test_tpl", recipient=self.user, context=ctx, agg_context="k")
        self.assertNotEqual(a.pk, b.pk)

    def test_emit_inactive_template_rejected(self):
        """模板停用拒发并返回 None。"""
        make_template(code="off_tpl", is_active=False)
        self.assertIsNone(services.emit("off_tpl", recipient=self.user, context={}))

    def test_announce_bulk(self):
        """公告批量推送：全部启用用户（含触发人）各一条；站内即达直接 sent + 渠道日志；同批 batch_id。"""
        User.objects.create_user("u2", password="x12345678")
        admin = User.objects.create_superuser("boss", password="x12345678")
        count = services.announce("维护公告", "今晚维护", created_by=admin)
        self.assertEqual(count, 3)
        rows = Notification.objects.filter(category="announcement")
        self.assertEqual(rows.count(), 3)
        self.assertTrue(
            Notification.objects.filter(created_by=admin, title="维护公告").exists()
        )
        # 站内即达：无邮件需求副本直接 sent，且写 inapp 渠道日志
        for n in rows:
            self.assertEqual(n.status, Notification.Status.SENT)
            self.assertIsNotNone(n.sent_at)
            self.assertTrue(n.deliveries.filter(channel="inapp", status="success").exists())
        # 同批副本共享 batch_id
        self.assertEqual(len(set(rows.values_list("batch_id", flat=True))), 1)

    def test_announce_revoke_whole_batch(self):
        """公告撤回整批生效：撤一条，全部接收者副本同批 revoked。"""
        User.objects.create_user("u2", password="x12345678")
        admin = User.objects.create_superuser("boss", password="x12345678")
        services.announce("测试撤回公告", "内容", created_by=admin)
        rows = list(Notification.objects.filter(category="announcement"))
        self.assertEqual(len(rows), 3)
        # 触发人撤回自己那份 → 整批撤回
        revoked = services.revoke(rows[0], admin)
        self.assertEqual(revoked, 3)
        self.assertEqual(
            Notification.objects.filter(category="announcement", status=Notification.Status.REVOKED).count(),
            3,
        )
        # 已撤回副本对接收者不可见（以 u2 视角验证）
        u2 = User.objects.get(username="u2")
        visible = Notification.objects.exclude(
            status__in=[Notification.Status.REVOKED, Notification.Status.EXPIRED]
        ).filter(recipient=u2)
        self.assertEqual(visible.count(), 0)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class ChannelDispatchTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("u1", password="x12345678", email="u1@test.com")
        self.tpl = make_template()
        self.notif = services.emit("test_tpl", recipient=self.user,
                                   context={"thing": "a", "date": timezone.now()})

    def _claim(self):
        self.assertTrue(services.claim_for_send(self.notif.pk))
        self.notif.refresh_from_db()
        return self.notif

    def test_dispatch_inapp_and_email(self):
        """默认偏好：站内 + 邮件两渠道均发送，日志落库，通知置 sent。"""
        from django.core.management import call_command

        call_command("notify_worker", "--once")
        self.notif.refresh_from_db()
        self.assertEqual(self.notif.status, Notification.Status.SENT)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("邮件 a", mail.outbox[0].subject)
        self.assertEqual(
            NotificationDelivery.objects.filter(notification=self.notif, status="success").count(), 2,
        )

    def test_email_disabled_by_pref(self):
        """偏好关闭邮件：仅站内渠道发送，邮件渠道被过滤（不落日志）。"""
        prefs = services.get_prefs(self.user)
        prefs.channel_prefs = {"task": {"email": False}}
        prefs.save()
        from django.core.management import call_command

        call_command("notify_worker", "--once")
        self.notif.refresh_from_db()
        self.assertEqual(self.notif.status, Notification.Status.SENT)
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(
            NotificationDelivery.objects.filter(notification=self.notif, channel="email").exists()
        )
        self.assertTrue(
            NotificationDelivery.objects.filter(notification=self.notif, channel="inapp",
                                                status="success").exists()
        )

    def test_email_no_address_permanent_fail(self):
        """收件人无邮箱：邮件渠道被 allowed_channels 前置过滤，仅站内即达置 sent。"""
        self.user.email = ""
        self.user.save()
        from django.core.management import call_command

        call_command("notify_worker", "--once")
        self.notif.refresh_from_db()
        self.assertEqual(self.notif.status, Notification.Status.SENT)
        self.assertFalse(self.notif.deliveries.filter(channel="email").exists())
        # 渠道兜底：直调 EmailChannel.send 仍返回不可重试错误
        from notifications.channels import EmailChannel

        result = EmailChannel().send(self.notif)
        self.assertTrue(result.permanent)
        self.assertIn("未配置邮箱", result.detail)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend")
    def test_smtp_failure_retries_with_backoff(self):
        """SMTP 失败：可重试错误回 pending，指数退避。"""
        from django.core.management import call_command

        call_command("notify_worker", "--once")
        self.notif.refresh_from_db()
        self.assertEqual(self.notif.status, Notification.Status.PENDING)
        self.assertEqual(self.notif.retry_count, 1)
        self.assertIsNotNone(self.notif.next_retry_at)
        # 退避时间 = 60s × 2^0 = 60s
        self.assertGreater(
            self.notif.next_retry_at, timezone.now() + timedelta(seconds=30),
        )

    def test_retry_exhausted_dead_letter(self):
        """重试 5 次后转死信。"""
        self._claim()
        Notification.objects.filter(pk=self.notif.pk).update(retry_count=5)
        self.notif.refresh_from_db()
        self.assertFalse(services.mark_retry(self.notif, "模拟失败"))
        self.notif.refresh_from_db()
        self.assertEqual(self.notif.status, Notification.Status.FAILED)

    def test_quiet_hours_skip_email_unless_high(self):
        """免打扰时段邮件跳过；高优先级穿透。

        免打扰区间用「现在 ± 30 分钟」构造。若直接取真实时间，在
        00:00–00:30 与 23:30–24:00 运行会跨日，区间变成 start > end，
        判断翻转导致假失败（CI 容器、新开机机器都会踩到）。
        这里把「现在」固定到当天 12:00，与真实时刻解耦。
        """
        from notifications.channels import allowed_channels

        # 注意：localdate() 内部也走 localtime()，必须在 patch 之前取到日期
        fixed_now = datetime.combine(timezone.localdate(), dtime(12, 0))
        with mock.patch("notifications.models.timezone.localtime") as fake_now:
            fake_now.return_value = fixed_now
            prefs = services.get_prefs(self.user)
            prefs.quiet_start = dtime(11, 30)
            prefs.quiet_end = dtime(12, 30)
            prefs.save()

            self.assertEqual(allowed_channels(self.notif), ["inapp"])
            self.notif.priority = "high"
            self.assertEqual(allowed_channels(self.notif), ["inapp", "email"])

    def test_template_without_email_support_skipped(self):
        """模板未配置邮件内容：邮件渠道被 allowed_channels 过滤。"""
        self.tpl.email_subject_template = None
        self.tpl.email_body_template = None
        self.tpl.save()
        from notifications.channels import allowed_channels

        # 重新取数，避开 notif 上缓存的旧模板对象
        notif = Notification.objects.select_related("template").get(pk=self.notif.pk)
        self.assertEqual(allowed_channels(notif), ["inapp"])


class WorkerLifecycleTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("u1", password="x12345678")
        self.tpl = make_template()

    def test_claim_is_atomic(self):
        """原子领取：第二次领取同一通知失败（防并发双发）。"""
        n = services.emit("test_tpl", recipient=self.user, context={})
        self.assertTrue(services.claim_for_send(n.pk))
        self.assertFalse(services.claim_for_send(n.pk))

    def test_expire_before_send(self):
        """过期通知：worker 领取前拦截置 expired。"""
        n = services.emit("test_tpl", recipient=self.user, context={},
                          expires_at=timezone.now() - timedelta(minutes=1))
        from django.core.management import call_command

        call_command("notify_worker", "--once")
        n.refresh_from_db()
        self.assertEqual(n.status, Notification.Status.EXPIRED)

    def test_expire_overdue_sweep(self):
        """notify_expire 兜底扫描过期 pending。"""
        services.emit("test_tpl", recipient=self.user, context={},
                      expires_at=timezone.now() - timedelta(minutes=1))
        from django.core.management import call_command

        call_command("notify_expire")
        self.assertEqual(
            Notification.objects.filter(status=Notification.Status.EXPIRED).count(), 1,
        )

    def test_requeue_dead_letter(self):
        """死信重投：failed → pending，重试计数清零。"""
        n = services.emit("test_tpl", recipient=self.user, context={})
        services.mark_failed(n, "模拟死信")
        from django.core.management import call_command

        call_command("notify_requeue", "--all-failed")
        n.refresh_from_db()
        self.assertEqual(n.status, Notification.Status.PENDING)
        self.assertEqual(n.retry_count, 0)


class IsolationTests(TestCase):
    """用户隔离：A/B 用户互不可见，越权 404。"""

    def setUp(self):
        self.a = User.objects.create_user("ua", password="x12345678")
        self.b = User.objects.create_user("ub", password="x12345678")
        grant_perms(self.a, "notifications.read")
        grant_perms(self.b, "notifications.read")
        self.tpl = make_template()
        self.n_a = services.emit("test_tpl", recipient=self.a, context={})
        self.n_b = services.emit("test_tpl", recipient=self.b, context={})
        self.client_a = Client()
        self.client_a.force_login(self.a)

    def test_list_only_own(self):
        resp = self.client_a.get(reverse("notifications:list"))
        self.assertEqual(resp.status_code, 200)
        # A 的列表能看到自己的通知标题，但不能看到 B 的（同一模板渲染相同内容，
        # 通过数量断言：A 仅 1 条）
        self.assertContains(resp, "共 1 条")

    def test_read_other_404(self):
        """A 标记 B 的通知已读 → 404。"""
        resp = self.client_a.post(reverse("notifications:read", args=[self.n_b.pk]))
        self.assertEqual(resp.status_code, 404)
        self.n_b.refresh_from_db()
        self.assertFalse(self.n_b.is_read)

    def test_unread_api_only_own(self):
        resp = self.client_a.get(reverse("notifications:unread_api"))
        data = resp.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["items"][0]["id"], self.n_a.pk)

    def test_mark_all_read_scope(self):
        services.emit("test_tpl", recipient=self.a, context={})
        count = services.mark_all_read(self.a)
        self.assertEqual(count, 2)
        self.n_b.refresh_from_db()
        self.assertFalse(self.n_b.is_read)  # B 不受影响

    def test_revoked_hidden_from_recipient(self):
        """撤回权限校验 + 撤回后接收者列表不可见。"""
        self.n_a.status = Notification.Status.SENT
        self.n_a.save()
        # b 无 system.update 权限，撤回失败
        self.assertEqual(services.revoke(self.n_a, self.b), 0)
        self.n_a.refresh_from_db()
        self.assertEqual(self.n_a.status, Notification.Status.SENT)
        # 超管可撤回；撤回后 A 的列表为空
        boss = User.objects.create_superuser("boss", password="x12345678")
        self.assertEqual(services.revoke(self.n_a, boss), 1)
        resp = self.client_a.get(reverse("notifications:list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "暂无通知")

    def test_revoke_rule_read_not_allowed(self):
        """已读通知不可撤回。"""
        self.n_a.status = Notification.Status.SENT
        self.n_a.save()
        services.mark_read(self.n_a)
        boss = User.objects.create_superuser("boss", password="x12345678")
        self.assertEqual(services.revoke(self.n_a, boss), 0)

    def test_permission_required(self):
        """无 notifications.read 权限访问通知中心 → 403。"""
        from accounts.models import Role

        stranger = User.objects.create_user("stranger", password="x12345678")
        Role.objects.create(code="norole", name="无通知权限", is_builtin=False)
        stranger.rbac_roles.add(Role.objects.get(code="norole"))
        client = Client()
        client.force_login(stranger)
        resp = client.get(reverse("notifications:list"))
        self.assertEqual(resp.status_code, 403)

    def test_preferences_one_to_one(self):
        resp = self.client_a.post(reverse("notifications:preferences"), {
            "email_task": "on", "agg_window_minutes": "10",
        })
        self.assertEqual(resp.status_code, 302)
        prefs = NotificationPreference.objects.get(user=self.a)
        self.assertFalse(prefs.email_enabled("business"))
        self.assertTrue(prefs.email_enabled("task"))
        self.assertEqual(prefs.agg_window_minutes, 10)

    def test_preferences_page_renders(self):
        """GET 偏好页正常渲染（模板不可带参调用 email_enabled，防回归）。"""
        resp = self.client_a.get(reverse("notifications:preferences"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "通知偏好")
        self.assertContains(resp, 'name="email_task"')
        self.assertContains(resp, "聚合降噪窗口")


class ScanCommandTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("u1", password="x12345678")

    def test_daily_missing_and_rerun_idempotent(self):
        """当日未填提醒：无记录用户收到通知；同日重跑不重复。"""
        from django.core.management import call_command

        call_command("check_daily_incomplete")
        self.assertEqual(
            Notification.objects.filter(template__code="daily_record_missing").count(), 1,
        )
        call_command("check_daily_incomplete")
        self.assertEqual(
            Notification.objects.filter(template__code="daily_record_missing").count(), 1,
        )

    def test_daily_overdue_incomplete(self):
        """过期未完成记录：归属用户收到提醒。"""
        from records.models import DailyRecord
        from django.core.management import call_command

        record = DailyRecord.objects.create(
            user=self.user, department="测试部", work_content="历史未完成",
            status="未完成", progress=30,
        )
        # record_date 为 auto_now_add，构造后强制回填为 2 天前
        DailyRecord.objects.filter(pk=record.pk).update(
            record_date=timezone.now() - timedelta(days=2),
        )
        call_command("check_daily_incomplete")
        self.assertEqual(
            Notification.objects.filter(template__code="daily_record_incomplete").count(), 1,
        )

    def test_insurance_expiry_notice(self):
        """保险临期：有 insurance.read 权限的用户收到提醒。"""
        from records.models import Insurance
        from django.core.management import call_command

        grant_perms(self.user, "insurance.read")
        Insurance.objects.create(
            company_name="测试保险", title="财产险", premium=100,
            issue_date=timezone.now().date() - timedelta(days=300),
            end_date=timezone.now().date() + timedelta(days=3),
            department="行政部",
        )
        call_command("check_expiry_notices")
        self.assertEqual(
            Notification.objects.filter(template__code="insurance_expiry").count(), 1,
        )


class RevokeLogTests(TestCase):
    """撤回记录页：批次聚合 / 已读未读统计 / 权限隔离。"""

    def setUp(self):
        self.admin = User.objects.create_superuser("boss", password="x12345678")
        self.staff = User.objects.create_user("staff", password="x12345678")
        User.objects.create_user("u1", password="x12345678")
        User.objects.create_user("u2", password="x12345678")
        # 一批公告：4 个接收者（admin/staff/u1/u2），u1 已读
        services.announce("撤回记录测试公告", "正文内容", created_by=self.admin)
        batch_rows = Notification.objects.filter(category="announcement")
        self.assertEqual(batch_rows.count(), 4)
        services.mark_read(batch_rows.get(recipient__username="u1"))
        # 触发人撤回整批（锚定本人未读副本，避免 queryset 无序取到已读条目）
        first = batch_rows.get(recipient=self.admin)
        self.assertEqual(services.revoke(first, self.admin), 4)

    def test_batch_aggregated_with_read_stats(self):
        """批次聚合为一行：显示内容、发送/撤回时间、已读未读统计与人员明细。"""
        c = Client()
        c.force_login(self.admin)
        resp = c.get(reverse("notifications:revoke_log"))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("撤回记录测试公告", body)
        self.assertIn("正文内容", body)
        self.assertIn("批量推送 · 4 人", body)
        # 统计块：接收 4 / 已读 1 / 未读 3
        self.assertIn('<div class="num">4</div>', body)
        self.assertIn("接收人数", body)
        self.assertIn('<div class="num">1</div>', body)
        self.assertIn('<div class="num">3</div>', body)
        self.assertIn("查看接收明细", body)

    def test_permission_denied_without_system_update(self):
        """无 system.update 权限（普通员工）访问撤回记录 → 403。"""
        c = Client()
        c.force_login(self.staff)
        resp = c.get(reverse("notifications:revoke_log"))
        self.assertEqual(resp.status_code, 403)

    def test_admin_button_hidden_for_staff(self):
        """通知列表页的管理按钮（推送/撤回记录）对普通员工隐藏。"""
        grant_perms(self.staff, "notifications.read")
        c = Client()
        c.force_login(self.staff)
        resp = c.get(reverse("notifications:list"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "推送系统公告")
        self.assertNotContains(resp, "撤回记录")


class ScheduledTaskTests(TestCase):
    """定时任务：种子数据 / 页面展示 / 超管配置与执行 / 到期判定。"""

    def setUp(self):
        self.superuser = User.objects.create_superuser("boss", password="x12345678")
        self.staff = User.objects.create_user("staff", password="x12345678")

    def test_seed_tasks_exist(self):
        """迁移种子：4 个内置任务存在且默认触发配置正确。"""
        from notifications.models import ScheduledTask

        codes = set(ScheduledTask.objects.values_list("code", flat=True))
        self.assertEqual(codes, {
            "check_expiry_notices", "check_daily_incomplete",
            "notify_expire", "notify_stats",
        })
        expiry = ScheduledTask.objects.get(code="check_expiry_notices")
        self.assertTrue(expiry.enabled)
        self.assertEqual(expiry.trigger_type, ScheduledTask.TriggerType.DAILY)
        self.assertEqual(expiry.params["days"], 7)
        self.assertEqual(ScheduledTask.objects.get(code="notify_stats").interval_minutes, 720)

    def test_list_page_shows_tasks(self):
        """通知中心页面展示定时任务区（含任务名与线程状态）。"""
        grant_perms(self.staff, "notifications.read")
        c = Client()
        c.force_login(self.staff)
        resp = c.get(reverse("notifications:list"))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("定时任务", body)
        self.assertIn("到期提醒扫描", body)
        self.assertIn("每日记录提醒", body)
        # 普通用户无配置/执行入口
        self.assertNotContains(resp, "立即执行")
        self.assertNotContains(resp, ">配置<")

    def test_update_and_run_require_superuser(self):
        """非超管配置/执行 → 403；未登录 → 302 跳登录。"""
        from notifications.models import ScheduledTask

        pk = ScheduledTask.objects.get(code="notify_expire").pk
        c = Client()
        c.force_login(self.staff)
        self.assertEqual(c.post(reverse("notifications:task_update", args=[pk]), {}).status_code, 403)
        self.assertEqual(c.post(reverse("notifications:task_run", args=[pk]), {}).status_code, 403)
        anon = Client()
        self.assertEqual(anon.post(reverse("notifications:task_run", args=[pk]), {}).status_code, 302)

    def test_update_by_superuser(self):
        """超管保存配置：触发方式/时间/参数/启停均落库。"""
        from notifications.models import ScheduledTask

        task = ScheduledTask.objects.get(code="check_expiry_notices")
        c = Client()
        c.force_login(self.superuser)
        resp = c.post(reverse("notifications:task_update", args=[task.pk]), {
            "enabled": "on", "trigger_type": "daily", "run_time": "08:30", "days": "14",
        })
        self.assertEqual(resp.status_code, 302)
        task.refresh_from_db()
        self.assertTrue(task.enabled)
        self.assertEqual(str(task.run_time), "08:30:00")
        self.assertEqual(task.params, {"days": 14})

        # 非法参数被拒绝（表单提交 → redirect + 错误 toast，配置不落库）
        resp = c.post(reverse("notifications:task_update", args=[task.pk]), {
            "enabled": "on", "trigger_type": "daily", "run_time": "08:30", "days": "999",
        })
        self.assertEqual(resp.status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.params["days"], 14)

    def test_run_by_superuser_records_result(self):
        """超管手动执行：命令真实运行，结果/耗时持久化，过期通知被兜底。"""
        from notifications.models import ScheduledTask

        user = User.objects.create_user("u1", password="x12345678")
        tpl = NotificationTemplate.objects.create(
            code="expire_tpl", name="过期模板", title_template="t", content_template="c",
            category="task", is_active=True,
        )
        Notification.objects.create(
            recipient=user, template=tpl, title="t", content="c",
            expires_at=timezone.now() - timedelta(hours=1),
        )
        task = ScheduledTask.objects.get(code="notify_expire")
        c = Client()
        c.force_login(self.superuser)
        resp = c.post(reverse("notifications:task_run", args=[task.pk]))
        self.assertEqual(resp.status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.last_status, "success")
        self.assertIn("过期兜底", task.last_message)
        self.assertIsNotNone(task.last_duration_ms)
        self.assertEqual(
            Notification.objects.get(pk=Notification.objects.first().pk).status,
            Notification.Status.EXPIRED,
        )

    def test_run_claim_prevents_concurrent(self):
        """执行中（running）的任务不允许重复领取执行。"""
        from notifications.models import ScheduledTask
        from notifications import scheduler

        task = ScheduledTask.objects.get(code="notify_expire")
        ScheduledTask.objects.filter(pk=task.pk).update(last_status="running")
        task.refresh_from_db()
        executed, ok, message = scheduler.run_task(task)
        self.assertFalse(executed)
        self.assertIn("执行中", message)

    def test_is_due_daily_and_interval(self):
        """到期判定：每日按当日时间点，间隔按上次执行时间推算。"""
        from datetime import timedelta as td

        from notifications.models import ScheduledTask
        from notifications import scheduler

        now = timezone.localtime()
        daily = ScheduledTask(
            code="x", name="x", enabled=True,
            trigger_type=ScheduledTask.TriggerType.DAILY,
            run_time=(now + td(minutes=2)).time(),
        )
        self.assertFalse(scheduler.is_due(daily))

        daily.run_time = (now - td(minutes=2)).time()
        self.assertTrue(scheduler.is_due(daily))

        # 「今天已跑过」必须锚定到今天的 run_time，而不是 now-1h：
        # 在 00:00~01:00 之间 now-1h 落在昨天，该断言会假失败（改造前实测过一次）。
        from datetime import datetime

        daily.last_run_at = timezone.make_aware(
            datetime.combine(now.date(), daily.run_time)
        )
        self.assertFalse(scheduler.is_due(daily))

        interval = ScheduledTask(
            code="y", name="y", enabled=True,
            trigger_type=ScheduledTask.TriggerType.INTERVAL,
            interval_minutes=60, last_run_at=timezone.now() - td(minutes=30),
        )
        self.assertFalse(scheduler.is_due(interval))
        interval.last_run_at = timezone.now() - td(minutes=90)
        self.assertTrue(scheduler.is_due(interval))

        # 停用任务永不到期
        interval.enabled = False
        self.assertFalse(scheduler.is_due(interval))


class TaskRunLogTests(TestCase):
    """批次B：定时任务执行历史表（TaskRunLog）写入与裁剪。"""

    def setUp(self):
        self.task = ScheduledTask.objects.get(code="notify_expire")

    def test_run_task_writes_run_log(self):
        """每次执行落一行历史：来源 / 结果 / 耗时 / 起止时间。"""
        from notifications import scheduler
        from notifications.models import TaskRunLog

        executed, _ok, _message = scheduler.run_task(self.task, source="manual")
        self.assertTrue(executed)

        log = TaskRunLog.objects.get(task=self.task)
        self.assertEqual(log.code, "notify_expire")
        self.assertEqual(log.source, TaskRunLog.Source.MANUAL)
        self.assertIn(log.status, ("success", "fail"))
        self.assertIsNotNone(log.duration_ms)
        self.assertNotEqual(log.message, "")
        self.assertLessEqual(log.started_at, log.finished_at)

    def test_run_log_source_defaults_to_schedule(self):
        """调度线程触发（未传 source）默认记为自动调度。"""
        from notifications import scheduler
        from notifications.models import TaskRunLog

        scheduler.run_task(self.task)
        self.assertEqual(TaskRunLog.objects.get().source, TaskRunLog.Source.SCHEDULE)

    def test_prune_keeps_latest_per_task(self):
        """历史裁剪：仅保留最近 RUN_LOG_KEEP_PER_TASK 条，更早的被删除。"""
        from datetime import timedelta as td

        from notifications import scheduler
        from notifications.models import TaskRunLog

        keep = scheduler.RUN_LOG_KEEP_PER_TASK
        base = timezone.now() - td(days=30)
        TaskRunLog.objects.bulk_create([
            TaskRunLog(
                task=self.task, code="notify_expire", source="schedule", status="success",
                duration_ms=1, message="x", started_at=base,
                finished_at=base + td(minutes=i),
            )
            for i in range(keep + 5)
        ])
        self.assertEqual(TaskRunLog.objects.filter(task=self.task).count(), keep + 5)

        scheduler._prune_run_logs(self.task.pk)

        remaining = TaskRunLog.objects.filter(task=self.task)
        self.assertEqual(remaining.count(), keep)
        # 保留的是最近的一批：最早的 5 条已删除
        self.assertEqual(remaining.order_by("finished_at").first().finished_at,
                         base + td(minutes=5))

    def test_prune_is_scoped_per_task(self):
        """裁剪只影响目标任务，其他任务的历史不受影响。"""
        from datetime import timedelta as td

        from notifications import scheduler
        from notifications.models import TaskRunLog

        other = ScheduledTask.objects.get(code="notify_stats")
        keep = scheduler.RUN_LOG_KEEP_PER_TASK
        now = timezone.now()
        TaskRunLog.objects.bulk_create([
            TaskRunLog(
                task=other, code="notify_stats", source="schedule", status="success",
                duration_ms=1, message="x", started_at=now, finished_at=now + td(minutes=i),
            )
            for i in range(3)
        ])
        TaskRunLog.objects.bulk_create([
            TaskRunLog(
                task=self.task, code="notify_expire", source="schedule", status="success",
                duration_ms=1, message="x", started_at=now, finished_at=now + td(minutes=i),
            )
            for i in range(keep + 2)
        ])

        scheduler._prune_run_logs(self.task.pk)

        self.assertEqual(TaskRunLog.objects.filter(task=self.task).count(), keep)
        self.assertEqual(TaskRunLog.objects.filter(task=other).count(), 3)

    def test_history_page_requires_system_read(self):
        """执行历史页面：仅 system.read 可访问，无权限 403，匿名 302。"""
        from notifications.models import TaskRunLog

        now = timezone.now()
        TaskRunLog.objects.create(
            task=self.task, code="notify_expire", source="schedule", status="success",
            duration_ms=5, message="执行成功", started_at=now, finished_at=now,
        )

        allowed = User.objects.create_user("th_ok", "th@e.com", "x12345678")
        grant_perms(allowed, "system.read")
        c = Client()
        c.force_login(allowed)
        resp = c.get(reverse("task_run_history"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["page_obj"].paginator.count, 1)

        denied = User.objects.create_user("th_no", "tn@e.com", "x12345678")
        c2 = Client()
        c2.force_login(denied)
        self.assertEqual(c2.get(reverse("task_run_history")).status_code, 403)

        self.assertEqual(Client().get(reverse("task_run_history")).status_code, 302)

    def test_history_page_filters(self):
        """结果筛选：status=fail 只返回失败记录。"""
        from notifications.models import TaskRunLog

        now = timezone.now()
        TaskRunLog.objects.create(
            task=self.task, code="notify_expire", source="schedule", status="fail",
            duration_ms=1, message="失败", started_at=now, finished_at=now,
        )
        TaskRunLog.objects.create(
            task=self.task, code="notify_expire", source="manual", status="success",
            duration_ms=1, message="成功", started_at=now, finished_at=now,
        )

        allowed = User.objects.create_user("th_flt", "tf@e.com", "x12345678")
        grant_perms(allowed, "system.read")
        c = Client()
        c.force_login(allowed)

        resp = c.get(reverse("task_run_history"), {"status": "fail"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["page_obj"].paginator.count, 1)

        resp = c.get(reverse("task_run_history"), {"task": str(self.task.pk)})
        self.assertEqual(resp.context["page_obj"].paginator.count, 2)

