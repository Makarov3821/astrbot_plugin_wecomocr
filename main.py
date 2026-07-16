"""严格限定为“单文件 OCR → 人工确认 → WPS 填表”的 AstrBot 插件。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Image
from astrbot.api.star import Context, Star, register

from .ocr_service import SUPPORTED_SUFFIXES, ocr_file
from .workflow import (
    format_fields,
    is_submit_command,
    parse_modifications,
    select_fields,
    submit_to_wps,
    validate_wps_url,
)


GUIDE = (
    "请一次上传一张 JPG、JPEG、PNG 图片或 PDF。识别后我会列出离校信息；"
    "你可以回复“姓名改为张三”（多个修改用换行或分号分隔），确认后回复“提交”。"
)


@dataclass
class ReviewSession:
    data: dict[str, str]
    updated_at: float


@register(
    "astrbot_plugin_wecomocr",
    "zyl",
    "派遣人员离校清单 OCR 确认与 WPS 填表",
    "1.0.0",
)
class WeComOCRPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._sessions: dict[str, ReviewSession] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def initialize(self) -> None:
        errors = self._configuration_errors()
        if errors:
            logger.warning("WeComOCR 插件配置未完成：%s", "；".join(errors))
        else:
            logger.info("WeComOCR 插件已加载，来源白名单数量：%d", len(self._allowed_sources()))

    def _config(self, key: str, default: Any = None) -> Any:
        try:
            return self.config.get(key, default)
        except AttributeError:
            return default

    def _string_list(self, key: str) -> list[str]:
        value = self._config(key, [])
        if isinstance(value, str):
            return [item.strip() for item in value.replace(",", "\n").splitlines() if item.strip()]
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return []

    def _allowed_sources(self) -> set[str]:
        return set(self._string_list("allowed_sources"))

    def _source_allowed(self, event: AstrMessageEvent) -> bool:
        source = str(event.unified_msg_origin)
        if source not in self._allowed_sources():
            if bool(self._config("debug", False)):
                logger.info("WeComOCR 忽略非白名单来源：%s", source)
            return False
        sender_allowlist = set(self._string_list("allowed_sender_ids"))
        return not sender_allowlist or str(event.get_sender_id()) in sender_allowlist

    def _session_key(self, event: AstrMessageEvent) -> str:
        return f"{event.unified_msg_origin}|{event.get_sender_id()}"

    def _configuration_errors(self) -> list[str]:
        errors: list[str] = []
        if not self._allowed_sources():
            errors.append("allowed_sources 为空")
        if not str(self._config("baidu_api_key", "")).strip():
            errors.append("baidu_api_key 为空")
        if not str(self._config("airscript_token", "")).strip():
            errors.append("airscript_token 为空")
        try:
            validate_wps_url(str(self._config("wps_script_url", "")))
        except ValueError as exc:
            errors.append(str(exc))
        return errors

    def _expire_session(self, key: str) -> bool:
        session = self._sessions.get(key)
        if session is None:
            return False
        minutes = max(1, int(self._config("session_timeout_minutes", 30)))
        if time.monotonic() - session.updated_at <= minutes * 60:
            return False
        self._sessions.pop(key, None)
        return True

    @staticmethod
    def _attachments(event: AstrMessageEvent) -> list[Image | File]:
        return [item for item in event.get_messages() if isinstance(item, (Image, File))]

    @staticmethod
    async def _attachment_path(component: Image | File) -> str:
        if isinstance(component, Image):
            return await component.convert_to_file_path()
        return await component.get_file()

    @staticmethod
    def _attachment_name(component: Image | File, path: str) -> str:
        declared = getattr(component, "name", None)
        return str(declared or Path(path).name)

    def _ocr(self, path: str) -> dict[str, Any]:
        return ocr_file(
            input_path=path,
            api_key=str(self._config("baidu_api_key", "")).strip(),
            model=str(self._config("baidu_model", "PaddleOCR-VL-1.6")),
            job_url=str(self._config("baidu_job_url", "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs")),
            poll_interval=float(self._config("ocr_poll_interval", 5.0)),
            job_timeout=int(self._config("ocr_job_timeout", 600)),
            request_timeout=int(self._config("ocr_request_timeout", 60)),
            debug=bool(self._config("debug", False)),
        )

    def _submit(self, data: dict[str, str]) -> dict[str, Any]:
        return submit_to_wps(
            url=str(self._config("wps_script_url", "")),
            token=str(self._config("airscript_token", "")).strip(),
            data=data,
            timeout=int(self._config("wps_request_timeout", 30)),
        )

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_workflow(self, event: AstrMessageEvent):
        """只处理配置来源内的离校信息 OCR 填表流程。"""
        if not self._source_allowed(event):
            return

        event.should_call_llm(False)
        event.stop_event()
        key = self._session_key(event)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            expired = self._expire_session(key)
            attachments = self._attachments(event)
            text = (event.message_str or "").strip()
            review = self._sessions.get(key)

            if review is not None:
                if attachments:
                    self._sessions.pop(key, None)
                    yield event.plain_result("确认阶段不能再次上传文件，本轮已清空。\n" + GUIDE)
                    return
                if is_submit_command(text):
                    if self._configuration_errors():
                        self._sessions.pop(key, None)
                        yield event.plain_result("插件配置不完整，无法提交；本轮已清空。请联系管理员。")
                        return
                    try:
                        await asyncio.to_thread(self._submit, review.data)
                    except Exception as exc:
                        logger.exception("WeComOCR WPS 提交失败：%s", exc)
                        self._sessions.pop(key, None)
                        yield event.plain_result("WPS 提交失败或结果不确定，本轮已清空；为避免重复填写，请联系管理员确认后再重新上传。")
                        return
                    self._sessions.pop(key, None)
                    yield event.plain_result("表格已填写完毕。本轮信息已清空，可以上传下一份文件。")
                    return
                try:
                    changes = parse_modifications(text)
                except ValueError as exc:
                    review.updated_at = time.monotonic()
                    yield event.plain_result(f"修改格式不正确：{exc}\n请重新修改，或回复“提交”。")
                    return
                if changes:
                    review.data.update(changes)
                    review.updated_at = time.monotonic()
                    yield event.plain_result(
                        "已修改，请再次确认：\n" + format_fields(review.data)
                        + "\n\n继续修改请使用“字段改为值”；确认无误请回复“提交”。"
                    )
                    return
                self._sessions.pop(key, None)
                yield event.plain_result("该消息不属于信息修改或提交指令，本轮已拒绝并清空。\n" + GUIDE)
                return

            if not attachments:
                if expired:
                    yield event.plain_result("上一轮已超时并清空。\n" + GUIDE)
                elif text in {"帮助", "使用说明", "开始", "/wecomocr"} or not text:
                    yield event.plain_result(GUIDE)
                else:
                    yield event.plain_result("该请求不属于离校信息填表流程，已拒绝。\n" + GUIDE)
                return

            if self._configuration_errors():
                yield event.plain_result("插件配置不完整，暂时无法处理文件，请联系管理员。")
                return
            first = attachments[0]
            ignored_notice = ""
            if len(attachments) > 1:
                ignored_notice = (
                    f"一次只能处理一个文件；已处理第 1 个，"
                    f"其余 {len(attachments) - 1} 个已忽略。\n\n"
                )
            try:
                path = await self._attachment_path(first)
                name = self._attachment_name(first, path)
                suffix = Path(name).suffix.lower() or Path(path).suffix.lower()
                if suffix not in SUPPORTED_SUFFIXES:
                    raise ValueError("仅支持 JPG、JPEG、PNG 图片或 PDF")
                result = await asyncio.to_thread(self._ocr, path)
                data = select_fields(result)
            except Exception as exc:
                logger.exception("WeComOCR OCR 处理失败：%s", exc)
                self._sessions.pop(key, None)
                yield event.plain_result("文件识别失败，本轮已清空。请确认文件格式和内容后重新上传；如仍失败请联系管理员。")
                return
            self._sessions[key] = ReviewSession(data=data, updated_at=time.monotonic())
            yield event.plain_result(
                ignored_notice + "识别完成，请确认以下信息：\n" + format_fields(data)
                + "\n\n需要修改时回复“字段改为值”，多个修改用换行或分号分隔；确认无误请回复“提交”。"
            )
            return

    async def terminate(self) -> None:
        self._sessions.clear()
        self._locks.clear()
