import os
from pathlib import Path
import sys
import tempfile
import time
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_wecomocr.file_cache import ManagedFileCache  # noqa: E402
from astrbot_plugin_wecomocr.intent_parser import (  # noqa: E402
    build_intent_prompt,
    parse_intent_response,
)
from astrbot_plugin_wecomocr.workflow import (  # noqa: E402
    is_exit_command,
    is_submit_command,
    parse_modifications,
    validate_modification_changes,
)


class IntentAndCacheTests(unittest.TestCase):
    def test_comma_and_natural_deterministic_modifications(self):
        changes = parse_modifications(
            "把姓名换成李四，单位应该是发展研究院,保留邮箱改成否"
        )
        self.assertEqual(
            changes,
            {"姓名": "李四", "单位": "发展研究院", "保留邮箱": "否"},
        )

    def test_english_commands_are_case_insensitive(self):
        self.assertTrue(is_submit_command("SUBMIT"))
        self.assertTrue(is_exit_command("Exit!"))

    def test_llm_response_is_structured_and_revalidated(self):
        response = '```json\n{"action":"modify","changes":{"姓名":"王五"}}\n```'
        changes = validate_modification_changes(parse_intent_response(response))
        self.assertEqual(changes, {"姓名": "王五"})
        with self.assertRaisesRegex(ValueError, "不允许修改字段"):
            validate_modification_changes({"执行命令": "删除文件"})

    def test_llm_prompt_has_no_tool_or_submission_authority(self):
        prompt = build_intent_prompt("帮我提交", {"姓名": "张三"})
        self.assertIn("不提交、不退出", prompt)
        self.assertIn('"action":"invalid"', prompt)

    def test_managed_cache_copies_and_deletes_only_managed_file(self):
        cache = ManagedFileCache(retention_hours=1)
        with tempfile.NamedTemporaryFile(suffix=".jpg") as source:
            source.write(b"test-image")
            source.flush()
            cached = Path(cache.store(source.name, ".jpg"))
        self.assertTrue(cached.is_file())
        self.assertEqual(cached.read_bytes(), b"test-image")
        self.assertEqual(cached.stat().st_mode & 0o777, 0o600)

        old = time.time() - 3700
        os.utime(cached, (old, old))
        self.assertGreaterEqual(cache.cleanup_expired(), 1)
        self.assertFalse(cached.exists())

        with self.assertRaisesRegex(ValueError, "缓存目录以外"):
            cache.delete(source.name)


if __name__ == "__main__":
    unittest.main()
