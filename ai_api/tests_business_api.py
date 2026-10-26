"""A 类业务页面 API 补齐的回归测试（周报/年报/到期/异常/下载/档案表级/文件夹）。"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from records.models import (
    DailyRecord, Insurance, InsuranceSubject, OperationLog,
)

User = get_user_model()

XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class BusinessApiTestBase(TestCase):
    def setUp(self):
        self.root = User.objects.create_superuser("biz_root", "biz@e.com", "x12345678")
        self.client = APIClient()
        # 会话认证：LoginRequiredMiddleware 读的是 Django request.user
        self.client.force_login(self.root)

    def _daily(self, user=None, status="完成", progress=100):
        return DailyRecord.objects.create(
            user=user or self.root, department="工程部",
            work_content="跟进装修进度", status=status, progress=progress,
        )

    def _insurance(self):
        today = timezone.localdate()
        return Insurance.objects.create(
            company_name="平安", title="财产一切险", premium="1200.00",
            issue_date=today, end_date=today + timedelta(days=10),
            department="行政部",
        )


class ReportApiTests(BusinessApiTestBase):
    def test_weekly_report_current_week(self):
        self._daily()
        resp = self.client.get("/api/daily-record/weekly-report/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["stats"]["total"], 1)
        self.assertIn("周报", body["report_md"])

    def test_weekly_report_is_user_isolated(self):
        other = User.objects.create_user("other_u", password="x12345678")
        self._daily(user=other)
        body = self.client.get("/api/daily-record/weekly-report/").json()
        self.assertEqual(body["stats"]["total"], 0)

    def test_annual_summary(self):
        self._daily()
        year = timezone.localdate().year
        resp = self.client.get(f"/api/daily-record/annual-summary/?year={year}")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["year"], year)
        self.assertEqual(body["total"], 1)

    def test_requires_permission(self):
        plain = User.objects.create_user("plain_u", password="x12345678")
        self.client.force_login(plain)
        resp = self.client.get("/api/daily-record/weekly-report/")
        self.assertEqual(resp.status_code, 403)


class ExpiryAndAuditApiTests(BusinessApiTestBase):
    def test_expiry_calendar_returns_items(self):
        self._insurance()
        resp = self.client.get("/api/expiry/calendar/?days=90")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertGreaterEqual(body["summary"]["total"], 1)
        self.assertTrue(any(g["items"] for g in body["by_day"]))

    def test_audit_anomalies_detects_delete_burst(self):
        for i in range(21):
            OperationLog.objects.create(
                operator="张三", operator_username="zhangsan",
                module_type="每日记录", operation_type="删除",
                object_identifier=f"记录#{i}",
            )
        resp = self.client.get("/api/operation-logs/anomalies/?days=7")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(len(body["delete_bursts"]), 1)
        self.assertEqual(body["delete_bursts"][0]["actor"], "zhangsan")

    def test_operation_logs_are_read_only(self):
        OperationLog.objects.create(
            operator="张三", operator_username="zhangsan",
            module_type="每日记录", operation_type="新增", object_identifier="记录#1",
        )
        resp = self.client.get("/api/operation-logs/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["count"], 1)
        denied = self.client.post("/api/operation-logs/", {}, format="json")
        self.assertEqual(denied.status_code, 405)


class DataDownloadApiTests(BusinessApiTestBase):
    def test_summary_counts(self):
        self._daily()
        self._insurance()
        resp = self.client.get("/api/data-download/summary/")
        self.assertEqual(resp.status_code, 200)
        counts = resp.json()["counts"]
        self.assertEqual(counts["daily_record"], 1)
        self.assertEqual(counts["insurance"], 1)
        self.assertIn("decoration", counts)


class InsuranceSubjectApiTests(BusinessApiTestBase):
    def test_subject_read_only_list(self):
        InsuranceSubject.objects.create(
            kind="car", label="粤B8X66D", plate="粤B8X66D", insurance_type="车险",
        )
        resp = self.client.get("/api/insurance/subjects/?kind=car")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["results"][0]["kind_label"], "车险")
        denied = self.client.post("/api/insurance/subjects/", {}, format="json")
        self.assertEqual(denied.status_code, 405)


class ArchiveTableApiTests(BusinessApiTestBase):
    def test_export_and_template(self):
        export = self.client.get("/api/archive/tables/archives/export/")
        self.assertEqual(export.status_code, 200)
        self.assertIn(XLSX_TYPE, export["Content-Type"])
        template = self.client.get("/api/archive/tables/archives/template/")
        self.assertEqual(template.status_code, 200)
        self.assertIn(XLSX_TYPE, template["Content-Type"])

    def test_unknown_table_404(self):
        resp = self.client.get("/api/archive/tables/nope/export/")
        self.assertEqual(resp.status_code, 404)

    def test_import_dry_run_and_commit(self):
        csv_body = "编号,名称,所属台账,类别,档案柜,层,格\nAPI-1,接口导入档案,,,,,\n"
        upload = SimpleUploadedFile("a.csv", csv_body.encode("utf-8-sig"),
                                    content_type="text/csv")
        preview = self.client.post(
            "/api/archive/tables/archives/import/",
            {"file": upload, "dry_run": "true"}, format="multipart",
        )
        self.assertEqual(preview.status_code, 200)
        body = preview.json()
        self.assertTrue(body["dry_run"])
        self.assertEqual(body["created"], 1)

        upload2 = SimpleUploadedFile("a.csv", csv_body.encode("utf-8-sig"),
                                     content_type="text/csv")
        commit = self.client.post(
            "/api/archive/tables/archives/import/",
            {"file": upload2, "dry_run": "false"}, format="multipart",
        )
        self.assertEqual(commit.status_code, 200)
        result = commit.json()
        self.assertFalse(result["dry_run"])
        self.assertEqual(result["created"], 1)


class KnowledgeFolderApiTests(BusinessApiTestBase):
    def test_folder_crud_and_audit(self):
        created = self.client.post("/api/knowledge/folders/", {"name": "根夹"},
                                   format="json")
        self.assertEqual(created.status_code, 201)
        root_id = created.json()["id"]

        child = self.client.post("/api/knowledge/folders/",
                                 {"name": "子夹", "parent": root_id}, format="json")
        self.assertEqual(child.status_code, 201)
        child_id = child.json()["id"]

        renamed = self.client.patch(f"/api/knowledge/folders/{child_id}/",
                                    {"name": "子夹2"}, format="json")
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.json()["name"], "子夹2")

        moved = self.client.post(f"/api/knowledge/folders/{child_id}/move/",
                                 {"parent_id": None}, format="json")
        self.assertEqual(moved.status_code, 200)
        self.assertIsNone(moved.json()["parent_id"])

        deleted = self.client.delete(f"/api/knowledge/folders/{root_id}/")
        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(deleted.json()["ok"])
        self.assertEqual(deleted.json()["moved_docs"], 0)

        self.assertTrue(OperationLog.objects.filter(
            module_type="知识库", object_identifier=f"文件夹#{child_id}").exists())

    def test_owner_isolation_returns_404(self):
        created = self.client.post("/api/knowledge/folders/", {"name": "私有夹"},
                                   format="json")
        folder_id = created.json()["id"]
        other_root = User.objects.create_superuser("biz_root2", "biz2@e.com", "x12345678")
        self.client.force_login(other_root)
        resp = self.client.get(f"/api/knowledge/folders/{folder_id}/")
        self.assertEqual(resp.status_code, 404)
