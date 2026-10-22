"""samples 模块文件上传校验器（SEC-M5 + R1）。

两份 clean_file（AuditRecordForm / ProofAttachmentForm）原先各自硬编码
扩展名白名单 + 大小上限，此处统一抽取，同时增加 magic byte（文件头）
与扩展名的双向校验。
"""

from django.conf import settings
from django.core.exceptions import ValidationError

# ---- 文件头 magic byte 映射（只覆盖常见格式，纯 Python 实现，不引入外部依赖） ----
# 仅校验扩展名 + magic 都在白名单内——扩展名可被伪造，magic byte 伪造成本高。
MAGIC_MAP: dict[str, bytes] = {
    # 图片（前 4 字节足够识别）
    ".jpg": b"\xff\xd8\xff",
    ".jpeg": b"\xff\xd8\xff",
    ".png": b"\x89PNG",
    ".gif": b"GIF8",
    ".webp": b"RIFF",    # webp 是 RIFF 容器，后续再校验第 9-12 字节
    ".bmp": b"BM",
    ".pdf": b"%PDF-",
    # 老版 Office（OLE 复合文档）：.doc/.xls/.ppt 三者 magic byte 相同
    ".doc": b"\xd0\xcf\x11\xe0",
    ".xls": b"\xd0\xcf\x11\xe0",
    ".ppt": b"\xd0\xcf\x11\xe0",
}

# 有 magic byte 的扩展名集合（新版 Office 是 zip、纯文本无 magic——都不列入）
HAS_MAGIC = set(MAGIC_MAP.keys())


def _check_magic(fileobj, ext: str) -> bool:
    """读取文件头校验 magic byte。仅对 HAS_MAGIC 扩展名生效。"""
    ext = ext.lower()
    if ext not in HAS_MAGIC:
        return True  # 无对应 magic byte 的扩展名跳过（信任扩展名）
    expected = MAGIC_MAP[ext]
    head = fileobj.read(len(expected))
    fileobj.seek(0)  # 必须复位，Django FileField 后续还要读
    if len(head) < len(expected):
        return False
    if ext == ".webp":
        # webp: RIFF....WEBP，head 里还应包含 "WEBP"
        if not head.startswith(b"RIFF"):
            return False
        # 再多读 4 字节验证 WEBP
        extra = fileobj.read(12)
        fileobj.seek(0)
        return b"WEBP" in extra
    return head.startswith(expected)


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
