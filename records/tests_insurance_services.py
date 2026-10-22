"""保险标的/保单年度落库与挂接服务（阶段 2 批 1/批 2）定向测试。"""

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from accounts.models import Permission, Role

from .insurance_services import (
    attach_record, cleanup_year, renewal_initial,
)
from .models import Insurance, InsurancePolicyYear, InsuranceSubject

User = get_user_model()


def grant(module, actions=("read",), user=None):
    role, _ = Role.objects.get_or_create(
        code=f"t_{module}", defaults={"name": f"测试-{module}", "is_active": True}
    )
    for action in actions:
        perm, _ = Permission.objects.get_or_create(
            module=module, action=action,
            defaults={"code": f"{module}:{action}", "name": f"{module}-{action}"},
        )
        role.permissions.add(perm)
    if user is not None:
        role.users.add(user)
        if hasattr(user, "_rbac_codes_cache"):
            del user._rbac_codes_cache
    cache.clear()
    return role


def make_insurance(title, end, premium="1000.00", issue=None, company="太平洋保险", dept="行政部"):
    return Insurance.objects.create(
        company_name=company, title=title, premium=Decimal(premium),
        issue_date=issue or date(end.year - 1, end.month, end.day),
        end_date=end, department=dept,
    )


class AttachRecordTests(TestCase):
    """attach_record：标的/年度创建、幂等、改挂自愈。"""

    def test_car_record_creates_subject_and_year(self):
        rec = make_insurance("车险 粤B8X66D", date(2027, 6, 10))
        subject, year = attach_record(rec)
        self.assertEqual(subject.kind, 'car')
        self.assertEqual(subject.plate, '粤B8X66D')
        self.assertEqual(subject.insurance_type, '车险')
        self.assertEqual(year.year, 2027)
        rec.refresh_from_db()
        self.assertEqual(rec.subject_id, subject.id)
        self.assertEqual(rec.policy_year_id, year.id)

    def test_same_subject_two_years_idempotent(self):
        rec1 = make_insurance("车险 粤B8X66D", date(2026, 6, 10), premium="1000.00")
        rec2 = make_insurance("车险 粤B8X66D", date(2027, 7, 20), premium="1100.00")
        attach_record(rec1)
        attach_record(rec2)
        attach_record(rec1)  # 幂等
        self.assertEqual(InsuranceSubject.objects.count(), 1)
        self.assertEqual(InsurancePolicyYear.objects.count(), 2)
        subject = InsuranceSubject.objects.get()
        y2026 = subject.policy_years.get(year=2026)
        self.assertEqual(str(y2026.premium), "1000.00")
        self.assertEqual(y2026.records.count(), 1)

    def test_multi_records_same_year_merged_and_recomputed(self):
        rec1 = make_insurance("财产一切险 宿舍", date(2025, 7, 18), premium="61224.11",
                              issue=date(2024, 7, 19))
        rec2 = make_insurance("财产一切险 总部", date(2025, 7, 18), premium="100000.00",
                              issue=date(2024, 7, 19))
        attach_record(rec1)
        attach_record(rec2)
        subject = InsuranceSubject.objects.get(insurance_type='财产一切险')
        year = subject.policy_years.get()
        self.assertEqual(year.records.count(), 2)
        self.assertEqual(str(year.premium), "161224.11")

    def test_edit_title_reattaches_and_cleans_old_year(self):
        rec = make_insurance("车险 粤B8X66D", date(2027, 6, 10))
        attach_record(rec)
        old_year_id = rec.policy_year_id
        # 标题改成另一台车：旧标的不删（人工主档保留），旧年度清空删除
        rec.title = "车险 粤B59Z88"
        rec.save()
        attach_record(rec)
        rec.refresh_from_db()
        self.assertEqual(rec.subject.plate, '粤B59Z88')
        self.assertFalse(InsurancePolicyYear.objects.filter(pk=old_year_id).exists())

    def test_edit_end_date_moves_to_new_year(self):
        rec = make_insurance("车险 粤B8X66D", date(2026, 6, 10))
        attach_record(rec)
        old_year_id = rec.policy_year_id
        rec.end_date = date(2027, 6, 10)
        rec.save()
        attach_record(rec)
        rec.refresh_from_db()
        self.assertEqual(rec.policy_year.year, 2027)
        self.assertFalse(InsurancePolicyYear.objects.filter(pk=old_year_id).exists())

    def test_cleanup_year_deletes_empty(self):
        rec = make_insurance("车险 粤B8X66D", date(2027, 6, 10))
        attach_record(rec)
        year_id = rec.policy_year_id
        Insurance.objects.filter(pk=rec.pk).delete()
        cleanup_year(year_id)
        self.assertFalse(InsurancePolicyYear.objects.filter(pk=year_id).exists())


class RenewalInitialTests(TestCase):
    """续保预填：日期顺推一年，单号清空。"""

    def test_dates_roll_one_year(self):
        rec = make_insurance("车险 粤B8X66D", date(2026, 6, 10),
                             issue=date(2025, 6, 11), premium="1000.00")
        initial = renewal_initial(rec)
        self.assertEqual(initial['issue_date'], date(2026, 6, 11))
        self.assertEqual(initial['end_date'], date(2027, 6, 10))
        self.assertEqual(initial['order_number'], '')
        self.assertEqual(initial['premium'], Decimal("1000.00"))
        self.assertEqual(initial['company_name'], rec.company_name)


class RenewalViewTests(TestCase):
    """续保视图：权限、GET 预填、POST 创建并挂接 + 留痕。"""

    def setUp(self):
        call_command("sync_rbac", verbosity=0)
        self.user = User.objects.create_user("ins_renew", "r@e.com", "x12345678")
        self.prev = make_insurance("车险 粤B8X66D", date(2026, 6, 10),
                                   issue=date(2025, 6, 11), premium="1000.00")
        # 模拟存量数据已回填的状态：上期保单已挂接标的/年度
        attach_record(self.prev)

    def test_create_permission_required(self):
        norole = User.objects.create_user("ins_norole", "n@e.com", "x12345678")
        self.client.force_login(norole)
        resp = self.client.get(reverse("insurance_renew"), {"from_record": self.prev.pk})
        self.assertEqual(resp.status_code, 403)

    def test_get_prefilled_form(self):
        grant("insurance", ("create",), self.user)
        self.client.force_login(self.user)
        resp = self.client.get(reverse("insurance_renew"), {"from_record": self.prev.pk})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "2026-06-11")
        self.assertContains(resp, "2027-06-10")

    def test_post_creates_attached_record_and_audit(self):
        grant("insurance", ("create",), self.user)
        self.client.force_login(self.user)
        resp = self.client.post(reverse("insurance_renew"), {
            "from_record": self.prev.pk,
            "company_name": "太平洋保险", "title": "车险 粤B8X66D",
            "order_number": "NEW-001", "premium": "1100.00",
            "issue_date": "2026-06-11", "end_date": "2027-06-10",
            "department": "行政部", "remarks": "",
        })
        self.assertRedirects(resp, reverse("insurance_subject_list"), fetch_redirect_response=False)
        new_rec = Insurance.objects.get(order_number="NEW-001")
        self.assertEqual(new_rec.subject.plate, "粤B8X66D")
        self.assertEqual(new_rec.policy_year.year, 2027)
        # 续保留痕
        from .models import OperationLog
        log = OperationLog.objects.filter(module_type="保险", operation_type="续保").latest("id")
        self.assertIn(str(new_rec.pk), log.object_identifier)
        # 两个年度都在同一标的上
        subject = new_rec.subject
        self.assertEqual(subject.policy_years.count(), 2)
