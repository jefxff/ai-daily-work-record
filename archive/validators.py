"""档案模块上传文件校验：批量导入 Excel 与档案附件共用。

审计 SEC-02 / SEC-08：Excel 导入入口此前只解析、不校验，超大或改名二进制
文件会被完整读入内存；档案附件只查扩展名，可上传「.pdf 实为 PE」的伪造文件。
本模块把扩展名白名单、大小上限、文件头（magic byte）三项收敛到一处，
避免各入口各写一份、逐步漂移。
"""

import os

from .models import ARCHIVE_FILE_ALLOWED_EXTS, ARCHIVE_FILE_MAX_SIZE

# 批量导入只接受表格文件：10MB 足够覆盖常见档案台账（远小于附件的 50MB）
IMPORT_ALLOWED_EXTS = {"XLSX", "XLS", "CSV"}
IMPORT_MAX_SIZE = 10 * 1024 * 1024

_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # 老版 Office 复合文档（doc/xls/ppt）

# 扩展名 -> 允许的文件头。Office 2007+ 与 zip 同为 PK 开头，
# 老版 Office 与 rar/7z 各有独立签名，据此挡住「改后缀」的二进制。
_ARCHIVE_MAGIC = {
    "PNG": (b"\x89PNG\r\n\x1a\n",),
    "JPG": (b"\xff\xd8\xff",),
    "JPEG": (b"\xff\xd8\xff",),
    "GIF": (b"GIF87a", b"GIF89a"),
    "BMP": (b"BM",),
    "WEBP": (b"RIFF",),
    "PDF": (b"%PDF",),
    "DOC": (_OLE_MAGIC,),
    "XLS": (_OLE_MAGIC,),
    "PPT": (_OLE_MAGIC,),
    "DOCX": _ZIP_MAGIC,
    "XLSX": _ZIP_MAGIC,
    "PPTX": _ZIP_MAGIC,
    "ZIP": _ZIP_MAGIC,
    "RAR": (b"Rar!\x1a\x07",),
    "7Z": (b"7z\xbc\xaf\x27\x1c",),
}


def _ext_of(f) -> str:
    return os.path.splitext(getattr(f, "name", "") or "")[1].lstrip(".").upper()


def _head(f, size: int = 16) -> bytes:
    """读取文件头后复位指针，避免影响后续解析器读取。"""
    head = f.read(size)
    try:
        f.seek(0)
    except (AttributeError, OSError):
        pass
    return head


def _looks_like_text(f) -> bool:
    """文本类文件：无 NUL 字节且能按 UTF-8/GBK 解码（CSV/TXT 共用）。"""
    head = _head(f, 4096)
    if b"\x00" in head:
        return False
    for enc in ("utf-8-sig", "gbk"):
        try:
            head.decode(enc)
            return True
        except UnicodeDecodeError:
            continue
    return False


def _magic_ok(ext: str, head: bytes) -> bool:
    if ext == "WEBP":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if ext == "TXT":
        return True  # 文本由 _looks_like_text 判定，无固定签名
    return any(head.startswith(prefix) for prefix in _ARCHIVE_MAGIC.get(ext, ()))


def validate_archive_file(f) -> str:
    """校验档案附件：扩展名 + 大小 + 文件头。通过返回空串，否则返回错误消息。"""
    ext = _ext_of(f)
    if ext not in ARCHIVE_FILE_ALLOWED_EXTS:
        return f"不支持的文件类型「{ext or '无扩展名'}」（允许：图片/PDF/Office/压缩包/文本）"
    if f.size > ARCHIVE_FILE_MAX_SIZE:
        return f"文件大小 {f.size / 1024 / 1024:.1f}MB 超过上限 50MB"
    if ext == "TXT":
        ok = _looks_like_text(f)
    else:
        ok = _magic_ok(ext, _head(f))
    if not ok:
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
    head = _head(f)
    if ext == "XLSX":
        ok = any(head.startswith(prefix) for prefix in _ZIP_MAGIC)
    elif ext == "XLS":
        ok = head.startswith(_OLE_MAGIC)
    else:
        ok = _looks_like_text(f)
    if not ok:
        raise ValueError("文件内容与扩展名不符，已拒绝解析")
