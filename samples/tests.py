"""samples 最小回归测试：编号唯一性 + 并发撞号重试闭环（H4）。

背景：sample_no / test_no / batch_no / audit_no 与 (sample|partner, round_no)
均已声明 unique 约束，作为并发撞号的最终兜底；但「取号 → 保存」之间存在
TOCTOU 竞态窗口，局域网两人同时新建时仍可能抛 IntegrityError 变成 500。
services.save_with_retry 用独立 savepoint 把该窗口用重试闭环补齐，
本文件即为该机制的回归测试。
"""

from datetime import date
from unittest import mock

from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from . import serialno, services
from .models import Sample


def _new_sample(material):
    return Sample(material_name=material, model="M-1", sample_date=date(2026, 1, 5))


class _NoSleepMixin:
    """屏蔽重试退避 sleep，让测试毫秒级完成。"""

    def setUp(self):
        super().setUp()
        patcher = mock.patch("samples.services.time.sleep", lambda *a, **k: None)
        patcher.start()
        self.addCleanup(patcher.stop)


class SerialNoUniquenessTests(TestCase):
    """编号唯一性：连续登记必须得到互不重复且递增的编号。"""

    def test_daily_serial_is_unique_and_incremental(self):
        first = _new_sample("物料A")
        services.create_sample(first, operator="测试员")
        second = _new_sample("物料B")
        services.create_sample(second, operator="测试员")

        today = timezone.localdate().strftime("%Y%m%d")
        self.assertEqual(first.sample_no, f"S{today}001")
        self.assertEqual(second.sample_no, f"S{today}002")
        self.assertNotEqual(first.sample_no, second.sample_no)

    def test_unique_constraint_still_backs_up(self):
        """唯一约束为最终兜底：绕过取号直插重复编号应被数据库拒绝。"""
        taken = _new_sample("物料A")
        services.create_sample(taken, operator="测试员")

        duplicate = Sample(
            sample_no=taken.sample_no, material_name="物料B",
            model="M-1", sample_date=date(2026, 1, 5),
        )
        with self.assertRaises(IntegrityError):
            duplicate.save()


class SaveWithRetryTests(_NoSleepMixin, TestCase):
    """并发撞号重试：冲突时重新取号；重试耗尽仍抛出原异常。"""

    def test_retries_once_then_succeeds(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise IntegrityError("模拟首次撞号")
            return "ok"

        self.assertEqual(services.save_with_retry(flaky), "ok")
        self.assertEqual(calls["n"], 2)

    def test_raises_after_exhausting_retries(self):
        def always_collide():
            raise IntegrityError("始终撞号")

        with self.assertRaises(IntegrityError):
            services.save_with_retry(always_collide, retries=3)

    def test_real_unique_violation_is_retried(self):
        """真实触发数据库唯一约束（跳过 full_clean），验证 savepoint 回滚后重试成功。"""
        taken = _new_sample("物料A")
        services.create_sample(taken, operator="测试员")

        attempts = {"n": 0}

        def assign_and_save():
            attempts["n"] += 1
            if attempts["n"] == 1:
                Sample(
                    sample_no=taken.sample_no, material_name="物料B",
                    model="M-1", sample_date=date(2026, 1, 5),
                ).save()
            return "ok"

        self.assertEqual(services.save_with_retry(assign_and_save), "ok")
        self.assertEqual(attempts["n"], 2)


class CreateSampleRetryIntegrationTests(_NoSleepMixin, TestCase):
    """create_sample 整体接入重试：命中撞号后可自愈，最终编号唯一。"""

    def test_create_sample_recovers_from_collision(self):
        taken = _new_sample("物料A")
        services.create_sample(taken, operator="测试员")

        real_generator = serialno.next_daily_serial
        calls = {"n": 0}

        def flaky_generator(model, field, prefix, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return taken.sample_no  # 首次返回已占用编号，制造真实撞号
            return real_generator(model, field, prefix, *args, **kwargs)

        # 关闭 full_clean 的唯一性前置校验，让冲突真正落到数据库约束上
        with mock.patch("samples.services.serialno.next_daily_serial", flaky_generator), \
                mock.patch.object(Sample, "full_clean", lambda self, *a, **k: None):
            second = _new_sample("物料B")
            services.create_sample(second, operator="测试员")

        self.assertEqual(calls["n"], 2)
        self.assertNotEqual(second.sample_no, taken.sample_no)
        self.assertEqual(Sample.objects.filter(sample_no=second.sample_no).count(), 1)
