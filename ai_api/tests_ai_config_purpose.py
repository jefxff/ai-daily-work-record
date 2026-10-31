"""AI 配置「按用途独立维护」的回归测试。

背景（2026-10-31 现象）：AI 助手徽章一直显示「云端模型」，客诉 / 诉讼 / 保险
等敏感模块一律被拦，且按提示去「系统管理 → AI 配置」也改不掉。根因有两层：

1. 配置页只会编辑 ``current()``（= 文本润色那一行），没有用途切换，也没有
   「本地模型」开关，用户**没有任何入口**把问答切到本地；
2. ``for_purpose`` 有跨用途回退，配置页一旦用它渲染，「对话问答」页显示的
   是润色的云端参数，用户以为改了问答模型，实际改的还是润色。

这里锁定修复后的语义：
- 配置页支持 purpose=polish / chat，两者互不覆盖；
- 某用途没有配置时给该用途的默认空白实例，不回退显示另一种用途的值；
- 「本地模型」「支持原生函数调用」能在页面上设置并落库；
- 重置只清当前用途；
- bootstrap 如实报告「借用了哪个用途的配置」，而不是假装问答已有配置。
"""

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from ai_api.models import AiProviderConfig

LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
User = get_user_model()


def _post_data(**overrides):
    data = {
        "provider": "ollama",
        "base_url": "http://127.0.0.1:11434",
        "model": "qwen2.5:7b",
        "api_key": "",
        "temperature": "0.2",
        "max_tokens": "1200",
        "timeout": "60",
        "is_local": "on",
        "is_enabled": "on",
    }
    data.update(overrides)
    return data


class ConfigPagePurposeTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_superuser("cfg_purpose_root", "p@e.com", "x12345678")
        self.client.force_login(self.user)
        self.polish = AiProviderConfig.objects.create(
            purpose=AiProviderConfig.Purpose.POLISH,
            provider=AiProviderConfig.Provider.OPENAI_COMPATIBLE,
            base_url="https://api.chenn.cn/v1",
            model="qwen2.5-vl-7b-instruct-awq",
            is_local=False,
            is_enabled=True,
        )

    def test_chat_page_does_not_show_polish_values(self):
        """没有 chat 配置时页面必须给空白实例，不能回显润色的云端参数。"""
        resp = self.client.get(reverse("ai_config"), {"purpose": "chat"})
        html = resp.content.decode()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["purpose"], "chat")
        self.assertFalse(resp.context["config"].pk)              # 未保存实例
        self.assertNotEqual(resp.context["config"].model, "qwen2.5-vl-7b-instruct-awq")
        self.assertNotIn('value="qwen2.5-vl-7b-instruct-awq"', html)
        # chat 默认面向本地：勾选本地模型
        self.assertTrue(resp.context["config"].is_local)

    def test_chat_page_tells_user_about_borrowed_config(self):
        resp = self.client.get(reverse("ai_config"), {"purpose": "chat"})
        effective = resp.context["chat_effective"]
        self.assertTrue(effective["borrowed"])
        self.assertEqual(effective["purpose_label"], "文本润色")
        self.assertIn("借用了", resp.content.decode())

    def test_save_chat_creates_separate_row_and_keeps_polish_intact(self):
        self.client.post(reverse("ai_config_save"),
                         _post_data(purpose="chat", model="qwen2.5:14b"))
        chat = AiProviderConfig.exact_for(AiProviderConfig.Purpose.CHAT)
        self.assertIsNotNone(chat)
        self.assertEqual(chat.model, "qwen2.5:14b")
        self.assertTrue(chat.is_local)
        self.assertTrue(chat.is_enabled)
        self.polish.refresh_from_db()
        self.assertEqual(self.polish.model, "qwen2.5-vl-7b-instruct-awq")
        self.assertFalse(self.polish.is_local)

    def test_save_chat_does_not_update_polish_row(self):
        self.client.post(reverse("ai_config_save"),
                         _post_data(purpose="chat", model="llama3.1:8b"))
        self.polish.refresh_from_db()
        self.assertEqual(self.polish.model, "qwen2.5-vl-7b-instruct-awq")
        self.assertEqual(AiProviderConfig.objects.filter(
            purpose=AiProviderConfig.Purpose.CHAT).count(), 1)

    def test_supports_tools_flag_is_persisted(self):
        self.client.post(reverse("ai_config_save"),
                         _post_data(purpose="chat", supports_tools="on"))
        chat = AiProviderConfig.exact_for(AiProviderConfig.Purpose.CHAT)
        self.assertTrue(chat.supports_tools)

    def test_local_flag_can_be_unchecked(self):
        """去掉勾选的本地模型必须真的落库为 False，否则敏感模块会误放行。"""
        self.client.post(reverse("ai_config_save"), _post_data(purpose="chat"))
        chat = AiProviderConfig.exact_for(AiProviderConfig.Purpose.CHAT)
        self.assertTrue(chat.is_local)
        data = _post_data(purpose="chat", is_local="")   # 不勾选
        self.client.post(reverse("ai_config_save"), data)
        chat.refresh_from_db()
        self.assertFalse(chat.is_local)

    def test_reset_is_scoped_to_purpose(self):
        self.client.post(reverse("ai_config_save"), _post_data(purpose="chat"))
        self.client.post(reverse("ai_config_reset"), {"purpose": "chat"})
        self.assertFalse(AiProviderConfig.objects.filter(
            purpose=AiProviderConfig.Purpose.CHAT).exists())
        self.assertTrue(AiProviderConfig.objects.filter(pk=self.polish.pk).exists())

    def test_reset_without_purpose_still_targets_polish(self):
        """旧页面/书签没有 purpose 字段时，仍然按历史语义重置润色。"""
        self.client.post(reverse("ai_config_reset"), {})
        self.assertFalse(AiProviderConfig.objects.filter(pk=self.polish.pk).exists())

    def test_invalid_purpose_falls_back_to_polish(self):
        resp = self.client.get(reverse("ai_config"), {"purpose": "hack"})
        self.assertEqual(resp.context["purpose"], AiProviderConfig.Purpose.POLISH)

    def test_tabs_report_each_purpose_state(self):
        resp = self.client.get(reverse("ai_config"))
        tabs = {t["value"]: t for t in resp.context["tabs"]}
        self.assertTrue(tabs["polish"]["enabled"])
        self.assertFalse(tabs["chat"]["configured"])


@override_settings(CACHES=LOCMEM)
class BootstrapModeTests(TestCase):
    """徽章为什么显示「云端模型」——必须能从接口里解释得清。"""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_superuser("boot_admin", "b@e.com", "x12345678")
        self.client.force_login(self.user)

    def test_borrowed_when_only_polish_configured(self):
        AiProviderConfig.objects.create(
            purpose=AiProviderConfig.Purpose.POLISH,
            provider=AiProviderConfig.Provider.OPENAI_COMPATIBLE,
            base_url="https://api.chenn.cn/v1",
            model="qwen2.5-vl-7b-instruct-awq",
            is_local=False, is_enabled=True,
        )
        data = self.client.get(reverse("copilot:bootstrap")).json()["data"]
        self.assertTrue(data["configured"])
        self.assertFalse(data["is_local"])
        self.assertTrue(data["borrowed"])
        self.assertEqual(data["borrowed_from"], "文本润色")
        self.assertIn("qwen2.5-vl-7b-instruct-awq", data["model"])

    def test_local_chat_config_wins_over_cloud_polish(self):
        AiProviderConfig.objects.create(
            purpose=AiProviderConfig.Purpose.POLISH,
            provider=AiProviderConfig.Provider.OPENAI_COMPATIBLE,
            base_url="https://api.chenn.cn/v1",
            model="cloud-model", is_local=False, is_enabled=True,
        )
        AiProviderConfig.objects.create(
            purpose=AiProviderConfig.Purpose.CHAT,
            provider=AiProviderConfig.Provider.OLLAMA,
            model="qwen2.5:7b", is_local=True, is_enabled=True,
        )
        data = self.client.get(reverse("copilot:bootstrap")).json()["data"]
        self.assertTrue(data["is_local"])
        self.assertFalse(data["borrowed"])
        self.assertEqual(data["model"], "qwen2.5:7b")
