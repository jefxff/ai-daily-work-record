"""文件上传校验的统一底座：扩展名归一、文件头（magic byte）判定、文本嗅探。

存在的原因（一致性审计 P1-1）：
同一件事（"这个文件真的是它声称的类型吗"）在仓库里写了两套表 + 一套副本：

- ``samples/validators.py::MAGIC_MAP``（PNG 只比对前 4 字节、PDF 比 ``%PDF-``）
- ``archive/validators.py::_ARCHIVE_MAGIC``（PNG 比 8 字节、PDF 比 ``%PDF``）
- ``archive/forms.py::_SCAN_MAGIC_PREFIXES``（第三份，仅图片/PDF）

同一张 PNG 在三个入口的判定宽严不同，改一处忘另两处就会出现"证照扫描件能传、
档案附件不能传"这种没法解释的现象。本模块只保留**一张表**，各模块的公开函数
（``validate_upload`` / ``validate_archive_file`` / ``validate_license_scan``）
保留自己的白名单、大小上限与错误文案，只把"文件头判定"委托到这里。

取舍：同名格式取**最严**的前缀（PNG 取 8 字节、GIF 取两种版本号），
这只会拒绝"改后缀的伪造文件"，不会误伤真实文件。
"""

import os

# ============================== 文件头表（唯一一份） ==============================

ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"   # 老版 Office 复合文档（doc/xls/ppt）

# 扩展名（大写、不带点）→ 允许的文件头前缀；空元组表示"无固定签名，单独判定"
MAGIC_PREFIXES = {
    "PNG": (b"\x89PNG\r\n\x1a\n",),
    "JPG": (b"\xff\xd8\xff",),
    "JPEG": (b"\xff\xd8\xff",),
    "GIF": (b"GIF87a", b"GIF89a"),
    "BMP": (b"BM",),
    "WEBP": (b"RIFF",),          # 还需校验第 8-12 字节为 WEBP，见 magic_ok
    "PDF": (b"%PDF",),
    "DOC": (OLE_MAGIC,),
    "XLS": (OLE_MAGIC,),
    "PPT": (OLE_MAGIC,),
    "DOCX": ZIP_MAGIC,
    "XLSX": ZIP_MAGIC,
    "PPTX": ZIP_MAGIC,
    "ZIP": ZIP_MAGIC,
    "RAR": (b"Rar!\x1a\x07",),
    "7Z": (b"7z\xbc\xaf\x27\x1c",),
    "TXT": (),
    "CSV": (),
}

# 有固定签名的扩展名（其余按"信任扩展名"处理）
HAS_MAGIC = frozenset(ext for ext, prefixes in MAGIC_PREFIXES.items() if prefixes)

# 需要按文本内容判定的扩展名
TEXT_EXTS = frozenset({"TXT", "CSV"})


def normalize_ext(name):
    """文件名 → 大写、不带点的扩展名（``a.PNG`` → ``PNG``）。"""
    ext = os.path.splitext(name or "")[1]
    return ext.lstrip(".").upper()


def read_head(fileobj, size=16):
    """读取文件头并**复位指针**（Django 的 UploadedFile 后面还要被解析器读）。"""
    head = fileobj.read(size)
    try:
        fileobj.seek(0)
    except (AttributeError, OSError):
        pass
    return head


def looks_like_text(fileobj):
    """文本类文件：前 4KB 无 NUL 字节，且能按 UTF-8(BOM)/GBK 解码。"""
    head = read_head(fileobj, 4096)
    if b"\x00" in head:
        return False
    for encoding in ("utf-8-sig", "gbk"):
        try:
            head.decode(encoding)
            return True
        except UnicodeDecodeError:
            continue
    return False


def magic_ok(ext, head):
    """文件头是否与扩展名相符。

    - 无签名的扩展名（TXT/CSV）：返回 True，由 ``looks_like_text`` 另行判定；
    - WEBP：``RIFF`` + 第 8-12 字节 ``WEBP``（只看 RIFF 会把 wav/avi 也放进来）；
    - 其余：任一允许前缀命中即通过。
    """
    ext = (ext or "").lstrip(".").upper()
    if ext in TEXT_EXTS:
        return True
    if ext == "WEBP":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    prefixes = MAGIC_PREFIXES.get(ext)
    if not prefixes:
        return True          # 未收录的扩展名不臆断，交给调用方的白名单兜底
    return any(head.startswith(prefix) for prefix in prefixes)


def content_matches(fileobj, ext, head=None):
    """组合判定：文本类看内容、其余看文件头。"""
    ext = (ext or "").lstrip(".").upper()
    if ext in TEXT_EXTS:
        return looks_like_text(fileobj)
    return magic_ok(ext, head if head is not None else read_head(fileobj))
