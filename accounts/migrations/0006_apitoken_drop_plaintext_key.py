# Generated for 审计 P1-2：Token 明文不落库

import hashlib

from django.conf import settings
from django.db import migrations, models


def _hash_key(raw: str) -> str:
    secret = settings.SECRET_KEY.encode("utf-8")
    return hashlib.sha256(raw.encode("utf-8") + secret).hexdigest()


def backfill_and_drop_plaintext(apps, schema_editor):
    """删除明文列前的两步收尾：补 key_hash、补 key_prefix。

    存量行若因某种原因 key_hash 为空（例如 0005 之后又手工插入过 key），
    这里按当前 SECRET_KEY 再算一次；随后把明文前缀搬去 key_prefix——
    这是明文的最后一次出境，删列后前端只能靠前缀辨认 Token。
    """
    ApiToken = apps.get_model("accounts", "ApiToken")
    for t in ApiToken.objects.all():
        updates = {}
        if not t.key_hash and t.key:
            updates["key_hash"] = _hash_key(t.key)
        if not t.key_prefix and t.key:
            updates["key_prefix"] = t.key[:12]
        if updates:
            ApiToken.objects.filter(pk=t.pk).update(**updates)


def restore_plaintext(apps, schema_editor):
    """反向迁移：无法还原已删除的明文，只能把列加回来（留空）。

    这一步注定丢数据，是本迁移"不留明文"设计的直接后果；反向迁移只为让
    rollback 能跑通，回滚后存量 Token 的 key_hash 仍可继续认证。
    """
    ApiToken = apps.get_model("accounts", "ApiToken")
    ApiToken.objects.update(key="")


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0005_apitoken_key_hash"),
    ]

    operations = [
        migrations.AddField(
            model_name="apitoken",
            name="key_prefix",
            field=models.CharField(
                blank=True,
                default="",
                help_text="明文 key 的前几位，仅用于用户在历史列表里辨认是哪一枚 Token",
                max_length=16,
                verbose_name="Token 前缀",
            ),
        ),
        migrations.RunPython(backfill_and_drop_plaintext, restore_plaintext),
        migrations.RemoveField(
            model_name="apitoken",
            name="key",
        ),
    ]
