import sys
from pathlib import Path
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_wecomocr.workflow import (  # noqa: E402
    build_wps_data,
    calculate_email_cancellation_date,
    parse_modifications,
    prepare_ocr_fields,
    submit_to_wps,
)


class WorkflowTests(unittest.TestCase):
    def test_cancellation_date_uses_calendar_days(self):
        self.assertEqual(
            calculate_email_cancellation_date("2026.07.16"), "2026.08.16"
        )
        self.assertEqual(
            calculate_email_cancellation_date("2025.01.31"), "2025.03.03"
        )
        self.assertEqual(calculate_email_cancellation_date("2026.02.30"), "无")

    def test_ocr_fields_include_calculated_seventh_field(self):
        data = prepare_ocr_fields(
            {
                "姓名": "张三",
                "复旦邮箱": "zhangsan@fudan.edu.cn",
                "离职日期": "2026-07-16",
                "保留邮箱": "否",
            }
        )
        self.assertEqual(data["离职日期"], "2026.07.16")
        self.assertEqual(data["邮箱注销日期"], "2026.08.16")
        self.assertEqual(len(data), 7)

    def test_keep_email_accepts_only_yes_or_no(self):
        self.assertEqual(parse_modifications("保留邮箱改为是"), {"保留邮箱": "是"})
        with self.assertRaisesRegex(ValueError, "只能填写"):
            parse_modifications("保留邮箱改为需要")

    def test_wps_mapping_when_email_is_kept(self):
        payload = build_wps_data(
            {
                "姓名": "张三",
                "工号": "20260001",
                "单位": "发展研究院",
                "复旦邮箱": "zhangsan@fudan.edu.cn",
                "离职日期": "2026.07.16",
                "保留邮箱": "是",
                "邮箱注销日期": "2026.08.16",
            }
        )
        self.assertEqual(payload["原院系单位"], "发展研究院")
        self.assertEqual(payload["保留姓名"], "张三")
        self.assertNotIn("单位", payload)
        self.assertNotIn("保留邮箱", payload)
        self.assertNotIn("邮箱注销日期", payload)

    def test_wps_mapping_when_email_is_not_kept(self):
        payload = build_wps_data(
            {
                "姓名": "张三",
                "工号": "20260001",
                "单位": "发展研究院",
                "复旦邮箱": "zhangsan@fudan.edu.cn",
                "离职日期": "2026.07.16",
                "保留邮箱": "否",
                "邮箱注销日期": "2026.08.20",
            }
        )
        self.assertEqual(payload["邮箱注销日期"], "2026.08.20")
        self.assertNotIn("保留姓名", payload)
        self.assertNotIn("单位", payload)
        self.assertNotIn("保留邮箱", payload)

    def test_submit_uses_mapped_payload(self):
        class Response:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"status": "finished", "data": {"result": None}}

        class Session:
            call = None

            def post(self, *args, **kwargs):
                self.call = (args, kwargs)
                return Response()

        session = Session()
        submit_to_wps(
            "https://365.kdocs.cn/a/sync_task",
            "test-token",
            {
                "姓名": "张三",
                "工号": "20260001",
                "单位": "发展研究院",
                "复旦邮箱": "zhangsan@fudan.edu.cn",
                "离职日期": "2026.07.16",
                "保留邮箱": "否",
                "邮箱注销日期": "2026.08.16",
            },
            session=session,
        )
        sent = session.call[1]["json"]["Context"]["argv"]["data"]
        self.assertEqual(sent["原院系单位"], "发展研究院")
        self.assertEqual(sent["邮箱注销日期"], "2026.08.16")


if __name__ == "__main__":
    unittest.main()
