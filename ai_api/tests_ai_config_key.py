"""AI 配置密钥保存链路的回归测试。

背景（2026-10-31 线上缺陷）：配置页把 ``api_key_masked`` 回填进密码框，
用户只改模型名、不动密钥直接保存时，提交上来的是掩码本身，旧实现把它加密
落库；此后 ``api_key_plain`` 返回的是 "sk...0236" 这种串，调模型鉴权必然
失败，而页面上掩码显示依旧正常，难以自查。

这里锁定修复后的语义：
- 密码框不再回填掩码（value 为空，掩码只出现在 placeholder）；
- 留空 = 不修改已保存的密钥；
- 旧页面/浏览器回填的掩码（与当前掩码逐字相同）= 视为未修改；
- 其它掩码形态 = 拒绝保存并提示；
- 显式「清除已保存的密钥」= 清空；
- 连通性测试在密钥留空时使用已存密钥，而不是掩码。
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .crypto import encrypt_key, looks_like_mask
from .models import AiProviderConfig

User = get_user_model()

REAL_KEY = "sk-real-0123456789abcdef"


class LooksLikeMaskTests(TestCase):
    def test_detects_mask_key_output(self):
        self.assertTrue(looks_like_mask("sk...0236"))
        self.assertTrue(looks_like_mask("Fm...*Abc"))

    def test_detects_star_placeholders(self):
        self.assertTrue(looks_like_mask("********"))
        self.assertTrue(looks_like_mask("sk-****0236"))

    def test_real_keys_are_not_masks(self):
        self.assertFalse(looks_like_mask(REAL_KEY))
        self.assertFalse(looks_like_mask("Fmdz@2027*Abc"))
        self.assertFalse(looks_like_mask(""))
        # 长度超过上限即使含省略号也不判定为掩码（真实密钥不会这么短）
        self.assertFalse(looks_like_mask("a" * 20 + "..."))


class AiConfigKeySaveTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser("cfg_key_root", "k@e.com", "x12345678")
        self.client.force_login(self.user)
        self.config = AiProviderConfig.objects.create(
            purpose=AiProviderConfig.Purpose.POLISH,
            provider=AiProviderConfig.Provider.OPENAI_COMPATIBLE,
            model="qwen2.5:7b",
            base_url="http://127.0.0.1:11434",
            is_enabled=True,
        )
        self.config.set_api_key(REAL_KEY)
        self.config.save()

    def _post(self, **overrides):
        data = {
            "provider": "openai_compatible",
            "base_url": "http://127.0.0.1:11434",
            "model": "qwen2.5:7b",
            "api_key": "",
            "temperature": "0.3",
            "max_tokens": "800",
            "timeout": "30",
            "is_enabled": "on",
        }
        data.update(overrides)
        return self.client.post(reverse("ai_config_save"), data)

    def test_page_does_not_prefill_masked_value(self):
        resp = self.client.get(reverse("ai_config"))
        html = resp.content.decode()
        self.assertEqual(resp.status_code, 200)
        self.assertIn('name="api_key" value=""', html)
        self.assertIn(self.config.api_key_masked, html)   # 仅出现在 placeholder / 提示里

    def test_blank_key_keeps_saved_key(self):
        self._post(api_key="", model="qwen2.5:14b")
        self.config.refresh_from_db()
        self.assertEqual(self.config.model, "qwen2.5:14b")
        self.assertEqual(self.config.api_key_plain, REAL_KEY)

    def test_submitted_mask_is_treated_as_unchanged(self):
        """旧页面缓存 / 浏览器回填：提交值与当前掩码一致 → 不覆盖密钥。"""
        self._post(api_key=self.config.api_key_masked)
        self.config.refresh_from_db()
        self.assertEqual(self.config.api_key_plain, REAL_KEY)

    def test_foreign_mask_is_rejected(self):
        resp = self._post(api_key="sk-****9999")
        self.config.refresh_from_db()
        self.assertEqual(resp.status_code, 200)            # 回显表单而非 302
        self.assertEqual(self.config.api_key_plain, REAL_KEY)

    def test_clear_checkbox_empties_key(self):
        self._post(clear_api_key="on")
        self.config.refresh_from_db()
        self.assertEqual(self.config.api_key_plain, "")

    def test_new_key_is_stored_encrypted(self):
        self._post(api_key="sk-new-9876543210abcdef")
        self.config.refresh_from_db()
        self.assertEqual(self.config.api_key_plain, "sk-new-9876543210abcdef")
        self.assertTrue(self.config.api_key.startswith("fernet:"))

    def test_set_api_key_refuses_mask(self):
        with self.assertRaises(ValueError):
            self.config.set_api_key("sk...0236")

    def test_test_endpoint_uses_saved_key_when_blank(self):
        captured = {}

        def fake_test(config):
            captured["plain"] = config.api_key_plain
            return True, "ok"

        with mock.patch("ai_api.views_config.test_connection", side_effect=fake_test):
            resp = self.client.post(reverse("ai_config_test"), {
                "provider": "openai_compatible",
                "base_url": "http://127.0.0.1:11434",
                "model": "qwen2.5:7b",
                "api_key": "",
            })
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        self.assertEqual(captured["plain"], REAL_KEY)

    def test_masked_dirty_data_is_flagged_on_page(self):
        """历史脏数据（库里就是掩码）在页面上要给出明确提示。"""
        AiProviderConfig.objects.filter(pk=self.config.pk).update(
            api_key=encrypt_key("sk...0236"))
        config = AiProviderConfig.objects.get(pk=self.config.pk)
        self.assertTrue(config.api_key_is_masked)

        resp = self.client.get(reverse("ai_config"))
        self.assertTrue(resp.context["api_key_is_masked"])
        self.assertIn("请重新输入完整密钥", resp.content.decode())

    def test_reset_only_clears_polish_config(self):
        """重置只清「文本润色」，不能连带删掉对话问答的配置。"""
        chat = AiProviderConfig.objects.create(
            purpose=AiProviderConfig.Purpose.CHAT,
            provider=AiProviderConfig.Provider.OLLAMA,
            model="qwen2.5:7b",
            is_enabled=True,
        )
        self.client.post(reverse("ai_config_reset"))
        self.assertFalse(AiProviderConfig.objects.filter(pk=self.config.pk).exists())
        self.assertTrue(AiProviderConfig.objects.filter(pk=chat.pk).exists())
