"""档案模块上传文件校验：批量导入 Excel 与档案附件共用。

审计 SEC-02 / SEC-08：Excel 导入入口此前只解析、不校验，超大或改名二进制
文件会被完整读入内存；档案附件只查扩展名，可上传「.pdf 实为 PE」的伪造文件。
本模块把扩展名白名单、大小上限、文件头（magic byte）三项收敛到一处，
避免各入口各写一份、逐步漂移。
"""

from work_record.file_validation import content_matches, normalize_ext, read_head

from .models import ARCHIVE_FILE_ALLOWED_EXTS, ARCHIVE_FILE_MAX_SIZE

# 批量导入只接受表格文件：10MB 足够覆盖常见档案台账（远小于附件的 50MB）
IMPORT_ALLOWED_EXTS = {"XLSX", "XLS", "CSV"}
IMPORT_MAX_SIZE = 10 * 1024 * 1024

# 文件头表统一在 work_record.file_validation（唯一一份）：
# 此前本文件与 samples/validators.py、archive/forms.py 各有一份，宽严不一。


def _ext_of(f) -> str:
    return normalize_ext(getattr(f, "name", "") or "")


def _head(f, size: int = 16) -> bytes:
    return read_head(f, size)


def validate_archive_file(f) -> str:
    """校验档案附件：扩展名 + 大小 + 文件头。通过返回空串，否则返回错误消息。"""
    ext = _ext_of(f)
    if ext not in ARCHIVE_FILE_ALLOWED_EXTS:
        return f"不支持的文件类型「{ext or '无扩展名'}」（允许：图片/PDF/Office/压缩包/文本）"
    if f.size > ARCHIVE_FILE_MAX_SIZE:
        return f"文件大小 {f.size / 1024 / 1024:.1f}MB 超过上限 50MB"
    if not content_matches(f, ext, _head(f)):
        return "文件内容与扩展名不符，已拒绝上传"
    return ""


def validate_import_file(f) -> None:
    """校验批量导入文件：扩展名 + 10MB 上限 + 文件头。不通过抛 ValueError。"""
    ext = _ext_of(f)
    if ext not in IMPORT_ALLOWED_EXTS:
        raise ValueError(
            f"不支持的文件类型「{ext or '无扩展名'}」（仅允许 .xlsx / .xls / .csv）"
        )
    if getattr(f, "size", 0) > IMPORT_MAX_SIZE:
        raise ValueError(
            f"文件大小 {f.size / 1024 / 1024:.1f}MB 超过上限 "
            f"{IMPORT_MAX_SIZE // 1024 // 1024}MB"
        )
    if not content_matches(f, ext, _head(f)):
        raise ValueError("文件内容与扩展名不符，已拒绝解析")
