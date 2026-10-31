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


# 掩码形态的判定上限：真实密钥远长于此，低于该长度且含省略号的只能是掩码
_MASK_MAX_LEN = 12


def looks_like_mask(value: str) -> bool:
    """判断一个字符串是不是「显示用的掩码」而非真实密钥。

    存在意义：配置页会把掩码回填到表单里，若浏览器连同掩码一起提交、或用户
    手工粘贴掩码，落库的就是掩码本身——之后解密得到的还是掩码，调模型必然
    鉴权失败，而且从页面上完全看不出问题（掩码显示「正常」）。所以在保存链路上
    必须能识别出这种情况。

    覆盖三种形态：
    1. ``mask_key`` 的产物：前 2 位 + ``...`` + 后 4 位，如 ``sk...0236``；
    2. 全星号占位：``********``；
    3. 手工/前端拼接的脱敏串，如 ``sk-****0236``。
    """
    text = (value or "").strip()
    if not text:
        return False
    if set(text) == {"*"}:
        return True
    if "****" in text:
        return True
    if "..." in text and len(text) <= _MASK_MAX_LEN:
        return True
    return False
