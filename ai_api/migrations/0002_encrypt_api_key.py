"""SEC-M2：AI 提供商 api_key 明文 → Fernet 加密回填。

加密格式：以 'fernet:' 开头，后跟 Fernet token（AES-128-CBC + HMAC-SHA256）。
密钥由项目 SECRET_KEY 派生：sha256(SECRET_KEY) → urlsafe base64。
"""

from django.db import migrations

from ai_api.crypto import decrypt_key, encrypt_key


def encrypt_api_keys(apps, schema_editor):
    """对存量明文 api_key 做一次性加密；已是 fernet: 前缀的跳过。"""
    AiProviderConfig = apps.get_model("ai_api", "AiProviderConfig")
    updated = 0
    for cfg in AiProviderConfig.objects.all():
        raw = cfg.api_key or ""
        if raw and not raw.startswith("fernet:"):
            encrypted = encrypt_key(raw)
            AiProviderConfig.objects.filter(pk=cfg.pk).update(api_key=encrypted)
            updated += 1


def decrypt_api_keys(apps, schema_editor):
    """反向迁移：把 fernet 前缀密文解密回明文。"""
    AiProviderConfig = apps.get_model("ai_api", "AiProviderConfig")
    for cfg in AiProviderConfig.objects.all():
        raw = cfg.api_key or ""
        if raw.startswith("fernet:"):
            plain = decrypt_key(raw)
            AiProviderConfig.objects.filter(pk=cfg.pk).update(api_key=plain)


class Migration(migrations.Migration):

    dependencies = [
        ("ai_api", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(encrypt_api_keys, decrypt_api_keys),
    ]
