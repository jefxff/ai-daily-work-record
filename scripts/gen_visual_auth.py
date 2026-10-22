"""生成 Playwright 登录态文件（tests/visual/.auth/state.json）。

只在本地/测试环境使用：直接复用现网超级管理员的会话，不修改业务数据。
用法：python scripts/gen_visual_auth.py
"""
import json
import os
import pathlib
import sys

import django

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "work_record.settings")
django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.contrib.sessions.backends.db import SessionStore  # noqa: E402

User = get_user_model()
user = User.objects.filter(is_superuser=True, is_active=True).order_by("id").first()
if user is None:
    raise SystemExit("没有可用的超级管理员账号")

session = SessionStore()
session["_auth_user_id"] = str(user.pk)
session["_auth_user_backend"] = "django.contrib.auth.backends.ModelBackend"
session["_auth_user_hash"] = user.get_session_auth_hash()
session.save()

cookie_name = "__Host-sessionid" if os.environ.get("DJANGO_HTTPS_ONLY") == "1" else "sessionid"
state = {
    "cookies": [
        {
            "name": cookie_name,
            "value": session.session_key,
            "domain": "127.0.0.1",
            "path": "/",
            "httpOnly": True,
            "secure": cookie_name.startswith("__Host-"),
            "sameSite": "Lax",
        }
    ],
    "origins": [],
}
target = pathlib.Path(__file__).resolve().parents[1] / "tests" / "visual" / ".auth" / "state.json"
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"写入 {target}（用户 {user.username}）")
