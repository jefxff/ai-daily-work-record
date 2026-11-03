"""每日记录列表页批量操作测试（规划书 drl-3）。

覆盖：批量标记完成、批量删除、批量导出选中（ids 参数）、
行级隔离（不能操作他人记录）、权限分别校验（update / delete）。
"""
import uuid

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from accounts.models import Permission, Role

from .models import DailyRecord

User = get_user_model()


def grant(module, actions=("read",), user=None):
    """每次调用生成独立角色：避免按模块 get_or_create 时动作叠加到他人角色上。"""
    role, _ = Role.objects.get_or_create(
        code=f"t_{module}_{uuid.uuid4().hex[:8]}",
        defaults={"name": f"测试-{module}", "is_active": True},
    )
    for action in actions:
        perm, _ = Permission.objects.get_or_create(
            module=module,
            action=action,
            defaults={"code": f"{module}:{action}", "name": f"{module}-{action}"},
        )
        role.permissions.add(perm)
    if user is not None:
        role.users.add(user)
        if hasattr(user, "_rbac_codes_cache"):
            del user._rbac_codes_cache
    cache.clear()
    return role


class DailyRecordBatchTests(TestCase):
    def setUp(self):
        call_command("sync_rbac", verbosity=0)
        self.user = User.objects.create_user("b_owner", "b@e.com", "x12345678")
        self.other = User.objects.create_user("b_other", "o@e.com", "x12345678")
        grant("daily_record", ("read", "create", "update", "delete"), self.user)
        self.client.force_login(self.user)
        self.mine_a = DailyRecord.objects.create(
            user=self.user, department="甲", work_content="事项A", status="未完成", progress=0,
        )
        self.mine_b = DailyRecord.objects.create(
            user=self.user, department="甲", work_content="事项B", status="进行中", progress=50,
        )
        self.others = DailyRecord.objects.create(
            user=self.other, department="乙", work_content="他人事项", status="未完成", progress=0,
        )

    def test_batch_complete_updates_selected_only(self):
        resp = self.client.post(
            reverse("daily_record_batch"),
            {"action": "complete", "ids": f"{self.mine_a.id},{self.mine_b.id}"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["updated"], 2)
        self.mine_a.refresh_from_db()
        self.mine_b.refresh_from_db()
        self.assertEqual(self.mine_a.status, "完成")
        self.assertEqual(self.mine_a.progress, 100)

    def test_batch_complete_skips_already_done(self):
        DailyRecord.objects.filter(pk=self.mine_a.pk).update(status="完成", progress=100)
        resp = self.client.post(
            reverse("daily_record_batch"), {"action": "complete", "ids": str(self.mine_a.id)}
        )
        self.assertEqual(resp.json()["updated"], 0, "已完成的记录不应重复计数")

    def test_batch_delete_removes_selected_only(self):
        resp = self.client.post(
            reverse("daily_record_batch"), {"action": "delete", "ids": str(self.mine_a.id)}
        )
        self.assertTrue(resp.json()["ok"])
        self.assertFalse(DailyRecord.objects.filter(pk=self.mine_a.pk).exists())
        self.assertTrue(DailyRecord.objects.filter(pk=self.mine_b.pk).exists())

    def test_batch_cannot_touch_other_users_records(self):
        """行级隔离：ids 里混入他人记录，必须被 _my_daily_records 过滤掉。"""
        resp = self.client.post(
            reverse("daily_record_batch"),
            {"action": "delete", "ids": str(self.others.id)},
        )
        self.assertEqual(resp.status_code, 404, "选中的 id 全部不可操作时应返回 404")
        self.assertTrue(DailyRecord.objects.filter(pk=self.others.pk).exists())

    def test_batch_requires_update_perm_for_complete(self):
        readonly = User.objects.create_user("b_ro", "r@e.com", "x12345678")
        grant("daily_record", ("read",), readonly)
        self.client.force_login(readonly)
        resp = self.client.post(
            reverse("daily_record_batch"), {"action": "complete", "ids": str(self.mine_a.id)}
        )
        self.assertEqual(resp.status_code, 403)

    def test_batch_requires_delete_perm_for_delete(self):
        editor = User.objects.create_user("b_ed", "e@e.com", "x12345678")
        grant("daily_record", ("read", "update"), editor)
        self.client.force_login(editor)
        resp = self.client.post(
            reverse("daily_record_batch"), {"action": "delete", "ids": str(self.mine_a.id)}
        )
        self.assertEqual(resp.status_code, 403)

    def test_batch_rejects_empty_and_unknown_action(self):
        resp = self.client.post(reverse("daily_record_batch"), {"action": "complete", "ids": ""})
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post(
            reverse("daily_record_batch"), {"action": "nope", "ids": str(self.mine_a.id)}
        )
        self.assertEqual(resp.status_code, 400)

    def test_export_supports_ids_filter(self):
        resp = self.client.get(reverse("daily_record_export"), {"ids": str(self.mine_a.id)})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("spreadsheetml", resp["Content-Type"])

    def test_export_ids_respect_ownership(self):
        """导出 ids 混入他人记录：导出的仍是自己的行，不抛异常也不泄数据。"""
        resp = self.client.get(
            reverse("daily_record_export"), {"ids": f"{self.others.id},{self.mine_a.id}"}
        )
        self.assertEqual(resp.status_code, 200)

    def test_list_page_has_batch_ui(self):
        html = self.client.get(reverse("daily_record_list")).content.decode()
        self.assertIn("drCheckAll", html)
        self.assertIn("dr-row-check", html)
        self.assertIn("drBatchBar", html)
