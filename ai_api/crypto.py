"""AI 密钥加解密工具（SEC-M2）。

基于 Fernet（AES-128-CBC + HMAC-SHA256），密钥由项目 SECRET_KEY 派生。
派生方式：sha256(SECRET_KEY) → base64 urlsafe → Fernet key。

不直接在数据库里改字段类型，而是对 AiProviderConfig.api_key 的
存取做透明加解密（property + setter），改动最小且向后兼容——迁移脚本
只需对存量明文做一次性加密。
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _derive_fernet_key() -> bytes:
    """从 SECRET_KEY 派生 Fernet key（sha256 digest → urlsafe base64）。"""
    digest = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


_fernet = Fernet(_derive_fernet_key())
_CRYPT_PREFIX = b"fernet:"  # 加密值统一以此前缀开头，用于识别 + 兼容明文


def encrypt_key(plaintext: str) -> str:
    """加密后返回以 'fernet:' 开头的密文字符串。空值原样返回。"""
    if not plaintext:
        return ""
    if plaintext.startswith("fernet:"):
        return plaintext  # 已加密，避免双重加密
    token = _fernet.encrypt(plaintext.encode("utf-8"))
    return (_CRYPT_PREFIX + token).decode("utf-8")


def decrypt_key(token: str) -> str:
    """解密 Fernet 密文。非加密格式（明文）原样返回——过渡期兼容。"""
    if not token:
        return ""
    if not token.startswith("fernet:"):
        return token  # 仍为明文（存量未迁移），透明返回
    try:
        raw = token[len("fernet:"):].encode("utf-8")
        return _fernet.decrypt(raw).decode("utf-8")
    except InvalidToken:
        # SECRET_KEY 变了或密文损坏——返回空字符串并记日志
        import logging
        logging.getLogger(__name__).warning("AI 密钥解密失败（可能 SECRET_KEY 已变更），返回空")
        return ""


def mask_key(token: str, *, keep_last: int = 4) -> str:
    """返回掩码显示，如 'sk-...****'。"""
    plain = decrypt_key(token)
    if not plain:
        return ""
    if len(plain) <= keep_last:
        return "*" * len(plain)
    return plain[:2] + "..." + plain[-keep_last:]
