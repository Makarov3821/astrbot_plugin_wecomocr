import sys
from pathlib import Path
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_wecomocr.workflow import apply_review_changes  # noqa: E402


class ReviewChangeTests(unittest.TestCase):
    def test_first_valid_departure_change_initializes_cancellation_date_once(self):
        initial = {
            "姓名": "张三",
            "复旦邮箱": "zhangsan@fudan.edu.cn",
            "离职日期": "无",
            "保留邮箱": "否",
            "邮箱注销日期": "无",
        }
        updated, initialized = apply_review_changes(
            initial, {"离职日期": "2026.07.16"}, False
        )
        self.assertTrue(initialized)
        self.assertEqual(updated["邮箱注销日期"], "2026.08.16")

        updated, initialized = apply_review_changes(
            updated, {"离职日期": "2026.07.20"}, initialized
        )
        self.assertTrue(initialized)
        self.assertEqual(updated["离职日期"], "2026.07.20")
        self.assertEqual(updated["邮箱注销日期"], "2026.08.16")

    def test_manual_cancellation_date_is_not_overwritten(self):
        initial = {
            "复旦邮箱": "zhangsan@fudan.edu.cn",
            "离职日期": "无",
            "保留邮箱": "否",
            "邮箱注销日期": "无",
        }
        updated, initialized = apply_review_changes(
            initial, {"邮箱注销日期": "2026.09.01"}, False
        )
        updated, initialized = apply_review_changes(
            updated, {"离职日期": "2026.07.20"}, initialized
        )
        self.assertTrue(initialized)
        self.assertEqual(updated["邮箱注销日期"], "2026.09.01")


if __name__ == "__main__":
    unittest.main()
