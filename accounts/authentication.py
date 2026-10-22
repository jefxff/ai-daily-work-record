"""DRF 认证：长期 Token（``Authorization: Token <key>``）。

与 SessionAuthentication 并存：请求头携带 Token 时优先按 Token 认证，
未携带时回退会话认证，浏览器页面请求不受任何影响。
认证成功后 request.user 即 Token 归属用户，后续 RBAC 判定（积极）与
Session 认证完全一致 —— 外部 Agent 无需人工在场即可完成身份校验。
"""

from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed

from .models import ApiToken, _hash_token_key


class ApiTokenAuthentication(BaseAuthentication):
    """按 ApiToken 表中的长期 Token 认证。

    优先比对 key_hash（sha256 加盐），找不到再 fallback 明文 key（过渡期兼容存量记录）。
    后续新签发 Token key_hash 必填，存量 Token 由迁移脚本批量回填。
    """

    keyword = "Token"

    def authenticate(self, request):
        auth = get_authorization_header(request).split()
        if not auth or auth[0].lower() != self.keyword.lower().encode():
            # 未携带 Token 头：交给后续认证类（会话认证）
            return None
        if len(auth) != 2:
            raise AuthenticationFailed("Token 请求头格式无效。")
        try:
            key = auth[1].decode()
        except UnicodeError:
            raise AuthenticationFailed("Token 请求头格式无效。")

        # 优先按 hash 比对（安全路径），fallback 到明文 key（过渡期兼容存量）
        h = _hash_token_key(key)
        token = (
            ApiToken.objects.select_related("user").filter(key_hash=h).first()
            or ApiToken.objects.select_related("user").filter(key=key).first()
        )
        if token is None or not token.is_valid:
            raise AuthenticationFailed("Token 无效或已过期。")
        if not token.user.is_active:
            raise AuthenticationFailed("账户不可用。")
        return (token.user, token)

    def authenticate_header(self, request):
        # 返回非空值时，认证失败将回 401（而非 403），便于调用方区分未认证/无权限
        return self.keyword
