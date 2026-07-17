import asyncio
import logging
from pathlib import Path
import sys
import types
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _install_astrbot_stubs():
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event_module = types.ModuleType("astrbot.api.event")
    components = types.ModuleType("astrbot.api.message_components")
    star_module = types.ModuleType("astrbot.api.star")

    class Filter:
        class EventMessageType:
            ALL = "all"

        @staticmethod
        def event_message_type(_event_type):
            return lambda function: function

    class Image:
        def __init__(self, path, name):
            self.path = path
            self.name = name

        async def convert_to_file_path(self):
            return self.path

    class File:
        async def get_file(self):
            return self.path

    class Star:
        def __init__(self, context):
            self.context = context

    class Context:
        pass

    def register(*_args):
        return lambda plugin_class: plugin_class

    api.AstrBotConfig = dict
    api.logger = logging.getLogger("wecomocr-test")
    event_module.AstrMessageEvent = object
    event_module.filter = Filter()
    components.Image = Image
    components.File = File
    star_module.Context = Context
    star_module.Star = Star
    star_module.register = register
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event_module,
            "astrbot.api.message_components": components,
            "astrbot.api.star": star_module,
        }
    )
    return Context, Image


Context, Image = _install_astrbot_stubs()

from astrbot_plugin_wecomocr.main import WeComOCRPlugin  # noqa: E402


class FakeEvent:
    def __init__(
        self,
        text="",
        messages=None,
        unified_msg_origin="wecom:private:one",
        platform_id=None,
    ):
        self.message_str = text
        self.messages = messages or []
        self.unified_msg_origin = unified_msg_origin
        self.platform_id = platform_id or unified_msg_origin.split(":", 1)[0]

    def get_messages(self):
        return self.messages

    def get_platform_id(self):
        return self.platform_id

    def get_sender_id(self):
        return "user-one"

    def should_call_llm(self, value):
        self.llm_enabled = value

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return text


async def collect(generator):
    return [message async for message in generator]


class PluginRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plugin = WeComOCRPlugin(
            Context(),
            {
                "allowed_sources": ["wecom:private:one"],
                "baidu_api_key": "test-key",
                "wps_script_url": "https://365.kdocs.cn/a/sync_task",
                "airscript_token": "test-token",
                "review_error_hint_threshold": 3,
                "enable_llm_intent_parsing": False,
            },
        )
        self.plugin._file_cache.store = lambda path, _suffix: path
        self.plugin._schedule_cache_deletion = lambda _path: None
        self.plugin._ocr = lambda _path: {
            "姓名": "张三",
            "工号": "20260001",
            "单位": "发展研究院",
            "复旦邮箱": "zhangsan@fudan.edu.cn",
            "离职日期": "2026.07.16",
            "保留邮箱": "否",
        }
        await collect(
            self.plugin.handle_workflow(
                FakeEvent(messages=[Image("/tmp/test.jpg", "test.jpg")])
            )
        )

    async def test_platform_instance_whitelist_takes_over_new_umos_only(self):
        plugin = WeComOCRPlugin(
            Context(),
            {"allowed_platform_ids": ["wecom-customer-service"]},
        )

        new_customer = FakeEvent(
            text="帮助",
            unified_msg_origin="wecom-customer-service:private:new-customer",
        )
        reply = await collect(plugin.handle_workflow(new_customer))
        self.assertTrue(new_customer.stopped)
        self.assertIn("请一次上传", reply[0])

        other_adapter = FakeEvent(
            text="帮助",
            unified_msg_origin="another-wecom:private:new-customer",
        )
        reply = await collect(plugin.handle_workflow(other_adapter))
        self.assertEqual(reply, [])
        self.assertFalse(hasattr(other_adapter, "stopped"))

    async def test_exact_source_whitelist_remains_compatible(self):
        allowed = FakeEvent(unified_msg_origin="wecom:private:one")
        denied = FakeEvent(unified_msg_origin="wecom:private:two")
        self.assertTrue(self.plugin._source_allowed(allowed))
        self.assertFalse(self.plugin._source_allowed(denied))

    async def test_errors_keep_session_and_exit_clears_it(self):
        key = "wecom:private:one|user-one"
        self.assertIn(key, self.plugin._sessions)

        for attempt in range(1, 4):
            reply = await collect(
                self.plugin.handle_workflow(FakeEvent(text="帮我查询天气"))
            )
            self.assertIn(key, self.plugin._sessions)
            if attempt < 3:
                self.assertNotIn("使用提示", reply[0])
            else:
                self.assertIn("使用提示", reply[0])

        reply = await collect(
            self.plugin.handle_workflow(FakeEvent(text="姓名改为李四"))
        )
        self.assertIn("姓名：李四", reply[0])
        self.assertEqual(self.plugin._sessions[key].error_count, 0)

        reply = await collect(
            self.plugin.handle_workflow(
                FakeEvent(messages=[Image("/tmp/other.jpg", "other.jpg")])
            )
        )
        self.assertIn("不能再次上传", reply[0])
        self.assertIn(key, self.plugin._sessions)

        reply = await collect(self.plugin.handle_workflow(FakeEvent(text="退出")))
        self.assertIn("已退出并清空", reply[0])
        self.assertNotIn(key, self.plugin._sessions)


if __name__ == "__main__":
    unittest.main()
