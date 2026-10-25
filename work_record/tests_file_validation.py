"""上传校验一致性测试（一致性审计 P1-1）。

回归来源：同一件事（文件头是否与扩展名相符）曾有三份表 ——
`samples/validators.py`、`archive/validators.py`、`archive/forms.py`，
PNG 一个查 4 字节一个查 8 字节、PDF 一个是 `%PDF-` 一个是 `%PDF`。
本文锁定「同一个文件在各入口必须得到同一个结论」这个不变量。
"""

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase

REAL_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64          # 完整 8 字节签名
TRUNCATED_PNG = b"\x89PNG" + b"\x00" * 64               # 只有前 4 字节
REAL_PDF = b"%PDF-1.7\n" + b"0" * 64
FAKE_PDF = b"MZ\x90\x00" + b"\x00" * 64                 # PE 头，改后缀伪装


def _file(name, content):
    return SimpleUploadedFile(name, content)


class UploadMagicConsistencyTests(SimpleTestCase):
    """样例入口与档案入口对同一文件给出一致判定。"""

    def _samples_ok(self, name, content):
        from samples.validators import validate_upload

        try:
            validate_upload(_file(name, content))
        except ValidationError:
            return False
        return True

    def _archive_ok(self, name, content):
        from archive.validators import validate_archive_file

        return validate_archive_file(_file(name, content)) == ""

    def test_real_files_accepted_everywhere(self):
        for name, content in (("a.png", REAL_PNG), ("a.pdf", REAL_PDF)):
            self.assertTrue(self._samples_ok(name, content), f"samples 应接受 {name}")
            self.assertTrue(self._archive_ok(name, content), f"archive 应接受 {name}")

    def test_fake_files_rejected_everywhere(self):
        for name, content in (("evil.pdf", FAKE_PDF), ("short.png", TRUNCATED_PNG)):
            self.assertFalse(self._samples_ok(name, content), f"samples 应拒绝 {name}")
            self.assertFalse(self._archive_ok(name, content), f"archive 应拒绝 {name}")

    def test_license_scan_uses_same_table(self):
        from archive.forms import validate_license_scan

        validate_license_scan(_file("scan.png", REAL_PNG))     # 不抛即通过
        with self.assertRaises(ValidationError):
            validate_license_scan(_file("scan.png", TRUNCATED_PNG))

    def test_single_magic_table(self):
        """三份表已合并为一份：模块里不应再有本地 magic 常量。"""
        import archive.forms as forms
        import archive.validators as av
        import samples.validators as sv

        self.assertFalse(hasattr(forms, "_SCAN_MAGIC_PREFIXES"))
        self.assertFalse(hasattr(av, "_ARCHIVE_MAGIC"))
        self.assertFalse(hasattr(sv, "MAGIC_MAP"))

        from work_record.file_validation import MAGIC_PREFIXES

        self.assertIn("PNG", MAGIC_PREFIXES)
        self.assertEqual(MAGIC_PREFIXES["PNG"][0], b"\x89PNG\r\n\x1a\n")
