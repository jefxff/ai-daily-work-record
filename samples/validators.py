"""samples 模块文件上传校验器（SEC-M5 + R1）。

两份 clean_file（AuditRecordForm / ProofAttachmentForm）原先各自硬编码
扩展名白名单 + 大小上限，此处统一抽取，同时增加 magic byte（文件头）
与扩展名的双向校验。
"""

from django.conf import settings
from django.core.exceptions import ValidationError

# 文件头表与判定统一在 work_record.file_validation（全站唯一一份）：
# 此前本模块、archive/validators.py、archive/forms.py 各有一份，PNG/PDF 的
# 前缀宽严还不一致（4 字节 vs 8 字节、%PDF- vs %PDF）。
from work_record.file_validation import (  # noqa: E402  （放在常量区，保持原有阅读顺序）
    HAS_MAGIC as _SHARED_HAS_MAGIC,
    magic_ok,
    read_head,
)

# 兼容本模块历史的“带点小写扩展名”写法（供 validate_upload 判断是否需要校验）
HAS_MAGIC = {f".{ext.lower()}" for ext in _SHARED_HAS_MAGIC}


def _check_magic(fileobj, ext: str) -> bool:
    """读取文件头校验 magic byte（实现见 work_record.file_validation.magic_ok）。"""
    return magic_ok(ext, read_head(fileobj, 16))


# 默认扩展名白名单（项目原两份 clean_file 的硬编码列表，去重后保留）
_DEFAULT_ALLOWED_EXT = [
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".txt", ".csv", ".zip", ".rar", ".7z",
]


def validate_upload(fileobj, *, allowed_ext=None, max_size=None,
                    field_label="文件", strict_magic=True):
    """统一上传校验：扩展名白名单 + 大小上限 + magic byte。

    Args:
        fileobj: Django File 对象
        allowed_ext: 扩展名白名单（默认 settings.PROOF_ALLOWED_EXT 或 _DEFAULT_ALLOWED_EXT）
        max_size: 大小上限字节数（默认 settings.PROOF_MAX_SIZE 或 20MB）
        field_label: 错误消息里用的字段名（'审核记录文件' / '证明文件' 等）
        strict_magic: True=对有 magic byte 的扩展名必须通过校验，False=只记录警告

    Raises:
        ValidationError: 校验不通过时抛出（包含 field_label）
    """
    if not fileobj:
        raise ValidationError(f"请上传{field_label}。")

    # 扩展名
    ext = (fileobj.name or "").rsplit(".", 1)
    ext = ("." + ext[1].lower()) if len(ext) == 2 else ""

    if allowed_ext is None:
        allowed_ext = list(getattr(settings, "PROOF_ALLOWED_EXT", []) or _DEFAULT_ALLOWED_EXT)
    if ext not in allowed_ext:
        raise ValidationError(
            f"不支持的文件类型「{ext or '未知'}」，支持图片、PDF、Office文档、文本、压缩包。"
        )

    # magic byte（双向比对，堵死"扩展名伪造"路径）
    if strict_magic and ext in HAS_MAGIC and not _check_magic(fileobj, ext):
        raise ValidationError(
            f"文件头与扩展名「{ext}」不匹配——请上传实际类型为「{ext}」的文件。"
        )

    # 大小
    if max_size is None:
        max_size = getattr(settings, "PROOF_MAX_SIZE", 20 * 1024 * 1024)
    if fileobj.size > max_size:
        mb = round(max_size / 1024 / 1024, 1)
        raise ValidationError(f"{field_label}大小超过上限（{mb}MB）。")

    return fileobj
