import sys
from pathlib import Path
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_wecomocr.workflow import (  # noqa: E402
    apply_review_changes,
    build_wps_data,
    format_fields,
    parse_modifications,
    prepare_ocr_fields,
)


class EmailEmptyLogicTests(unittest.TestCase):
    def test_email_alias_and_empty_synonyms(self):
        self.assertEqual(
            parse_modifications("邮箱改为无"), {"复旦邮箱": "无"}
        )
        self.assertEqual(
            parse_modifications("邮箱改为无邮箱"), {"复旦邮箱": "无"}
        )
        self.assertEqual(
            parse_modifications("EMAIL改为None"), {"复旦邮箱": "无"}
        )

    def test_ocr_without_email_has_blank_cancellation_date(self):
        data = prepare_ocr_fields(
            {
                "姓名": "张三",
                "复旦邮箱": "无",
                "离职日期": "2026.07.16",
                "保留邮箱": "否",
            }
        )
        self.assertEqual(data["邮箱注销日期"], "")
        self.assertIn("邮箱注销日期：\n", format_fields(data) + "\n")

    def test_changing_email_to_none_clears_cancellation_date(self):
        updated, initialized = apply_review_changes(
            {
                "复旦邮箱": "zhangsan@fudan.edu.cn",
                "离职日期": "2026.07.16",
                "保留邮箱": "否",
                "邮箱注销日期": "2026.08.16",
            },
            {"复旦邮箱": "无"},
            True,
        )
        self.assertEqual(updated["复旦邮箱"], "无")
        self.assertEqual(updated["邮箱注销日期"], "")
        self.assertFalse(initialized)

    def test_wps_receives_blank_cancellation_date_without_email(self):
        payload = build_wps_data(
            {
                "姓名": "张三",
                "工号": "20260001",
                "单位": "发展研究院",
                "复旦邮箱": "无",
                "离职日期": "2026.07.16",
                "保留邮箱": "否",
                "邮箱注销日期": "",
            }
        )
        self.assertEqual(payload["复旦邮箱"], "无")
        self.assertEqual(payload["邮箱注销日期"], "")


if __name__ == "__main__":
    unittest.main()
