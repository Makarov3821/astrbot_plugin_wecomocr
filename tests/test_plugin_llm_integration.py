from types import SimpleNamespace
import unittest


from test_plugin_recovery import Context, FakeEvent, Image, collect
from astrbot_plugin_wecomocr.main import WeComOCRPlugin


class PluginLLMIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_free_form_change_uses_llm_but_still_updates_only_allowed_field(self):
        context = Context()
        prompts = []

        async def get_provider(*, umo):
            self.assertEqual(umo, "wecom:private:one")
            return "provider-one"

        async def llm_generate(*, chat_provider_id, prompt):
            self.assertEqual(chat_provider_id, "provider-one")
            prompts.append(prompt)
            return SimpleNamespace(
                completion_text=(
                    '{"action":"modify","changes":{"姓名":"王五"}}'
                )
            )

        context.get_current_chat_provider_id = get_provider
        context.llm_generate = llm_generate
        plugin = WeComOCRPlugin(
            context,
            {
                "allowed_sources": ["wecom:private:one"],
                "baidu_api_key": "test-key",
                "wps_script_url": "https://365.kdocs.cn/a/sync_task",
                "airscript_token": "test-token",
                "enable_llm_intent_parsing": True,
            },
        )
        plugin._file_cache.store = lambda path, _suffix: path
        plugin._schedule_cache_deletion = lambda _path: None
        plugin._ocr = lambda _path: {
            "姓名": "张三",
            "工号": "20260001",
            "单位": "发展研究院",
            "复旦邮箱": "zhangsan@fudan.edu.cn",
            "离职日期": "2026.07.16",
            "保留邮箱": "否",
        }
        await collect(
            plugin.handle_workflow(
                FakeEvent(messages=[Image("/tmp/test.jpg", "test.jpg")])
            )
        )
        reply = await collect(
            plugin.handle_workflow(
                FakeEvent(text="之前登记的人名不对，正确的人其实叫王五")
            )
        )
        self.assertTrue(prompts)
        self.assertIn("姓名：王五", reply[0])
        key = "wecom:private:one|user-one"
        self.assertEqual(plugin._sessions[key].data["姓名"], "王五")


if __name__ == "__main__":
    unittest.main()
