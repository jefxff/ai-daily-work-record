"""附件上传校验：扩展名白名单 + 大小上限 + 文件头（magic byte）。

与 ``archive/validators.py::validate_archive_file`` 同构（通过返回空串、否则
返回错误消息，供上传视图逐个文件收集错误清单），复用
``work_record.file_validation`` 的唯一文件头表 —— 全站不再新写第二套 magic
byte 表。
"""

from work_record.file_validation import content_matches, normalize_ext, read_head

from .models import ATTACHMENT_ALLOWED_EXTS, ATTACHMENT_MAX_SIZE


def validate_attachment(f) -> str:
    """校验单个上传文件。通过返回空串，否则返回可直接展示的错误消息。"""
    ext = normalize_ext(getattr(f, "name", "") or "")
    if ext not in ATTACHMENT_ALLOWED_EXTS:
        return f"不支持的文件类型「{ext or '无扩展名'}」（允许：图片/PDF/Office/压缩包/文本）"
    size = getattr(f, "size", 0) or 0
    if size > ATTACHMENT_MAX_SIZE:
        return f"文件大小 {size / 1024 / 1024:.1f}MB 超过上限 {ATTACHMENT_MAX_SIZE // 1024 // 1024}MB"
    if not content_matches(f, ext, read_head(f)):
        return "文件内容与扩展名不符，已拒绝上传"
    return ""
