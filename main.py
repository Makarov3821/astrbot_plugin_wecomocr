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

from .file_cache import ManagedFileCache
from .intent_parser import build_intent_prompt, parse_intent_response
from .ocr_service import SUPPORTED_SUFFIXES, ocr_file
from .workflow import (
    apply_review_changes,
    build_wps_data,
    format_fields,
    is_exit_command,
    is_submit_command,
    prepare_ocr_fields,
    parse_modifications,
    submit_to_wps,
    validate_modification_changes,
    validate_wps_url,
)


GUIDE = (
    "请一次上传一张 JPG、JPEG、PNG 图片或 PDF。识别后我会列出离校信息；"
    "你可以回复“姓名改为张三”（多个修改用逗号或换行分隔），确认后回复“提交”；需要放弃本轮时回复“退出”。"
)


@dataclass
class ReviewSession:
    data: dict[str, str]
    updated_at: float
    cancellation_date_initialized: bool
    error_count: int = 0


@register(
    "astrbot_plugin_wecomocr",
    "zyl",
    "派遣人员离校清单 OCR 确认与 WPS 填表",
    "1.3.0",
)
class WeComOCRPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._sessions: dict[str, ReviewSession] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._file_cache = ManagedFileCache(
            retention_hours=float(self._config("cache_retention_hours", 1.0))
        )
        self._cache_tasks: set[asyncio.Task[None]] = set()
        self._cache_cleanup_task: asyncio.Task[None] | None = None

    async def initialize(self) -> None:
        deleted = await asyncio.to_thread(self._file_cache.cleanup_expired)
        if deleted:
            logger.info(f"WeComOCR 已清理 {deleted} 个过期缓存文件")
        self._cache_cleanup_task = asyncio.create_task(
            self._cache_cleanup_loop(), name="wecomocr-cache-cleanup"
        )
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

    async def _cache_cleanup_loop(self) -> None:
        interval_minutes = max(1.0, float(
            self._config("cache_cleanup_interval_minutes", 10.0)
        ))
        try:
            while True:
                await asyncio.sleep(interval_minutes * 60)
                try:
                    await asyncio.to_thread(self._file_cache.cleanup_expired)
                except Exception as exc:
                    logger.warning(f"WeComOCR 缓存清理失败：{exc}")
        except asyncio.CancelledError:
            raise

    def _schedule_cache_deletion(self, path: str) -> None:
        async def delete_later() -> None:
            try:
                delay = await asyncio.to_thread(
                    self._file_cache.seconds_until_expiry, path
                )
                if delay > 0:
                    await asyncio.sleep(delay)
                await asyncio.to_thread(self._file_cache.delete, path)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"WeComOCR 缓存文件删除失败：{exc}")

        task = asyncio.create_task(delete_later(), name="wecomocr-cache-delete")
        self._cache_tasks.add(task)
        task.add_done_callback(self._cache_tasks.discard)

    async def _parse_llm_modifications(
        self, event: AstrMessageEvent, text: str, data: dict[str, str]
    ) -> dict[str, str]:
        if not bool(self._config("enable_llm_intent_parsing", True)):
            return {}
        provider_id = await self.context.get_current_chat_provider_id(
            umo=event.unified_msg_origin
        )
        if not provider_id:
            return {}
        timeout = max(1, int(self._config("llm_intent_timeout", 30)))
        response = await asyncio.wait_for(
            self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=build_intent_prompt(text[:1000], data),
            ),
            timeout=timeout,
        )
        changes = parse_intent_response(response.completion_text)
        return validate_modification_changes(changes)

    def _review_error(
        self, review: ReviewSession, message: str
    ) -> str:
        review.error_count += 1
        review.updated_at = time.monotonic()
        lines = [message, "当前识别结果仍然保留，请重新输入。"]
        threshold = max(1, int(self._config("review_error_hint_threshold", 3)))
        if review.error_count >= threshold:
            lines.append(
                "使用提示：修改请回复“字段改为值”（多个修改用逗号或换行分隔）；"
                "确认请回复“提交”；放弃本轮请回复“退出”。"
            )
        return "\n".join(lines)

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
                if is_exit_command(text):
                    self._sessions.pop(key, None)
                    yield event.plain_result(
                        "已退出并清空本轮信息，可以重新上传一份文件。"
                    )
                    return
                if attachments:
                    yield event.plain_result(
                        self._review_error(
                            review,
                            "当前正在确认已识别的信息，不能再次上传文件。",
                        )
                    )
                    return
                if is_submit_command(text):
                    if self._configuration_errors():
                        yield event.plain_result(
                            "插件配置不完整，暂时无法提交；本轮信息仍然保留，请联系管理员。"
                        )
                        return
                    try:
                        build_wps_data(review.data)
                    except ValueError as exc:
                        yield event.plain_result(
                            self._review_error(
                                review, f"无法提交：{exc}。请先修改对应字段。"
                            )
                        )
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
                    yield event.plain_result(
                        self._review_error(review, f"修改格式不正确：{exc}")
                    )
                    return
                if not changes:
                    try:
                        changes = await self._parse_llm_modifications(
                            event, text, review.data
                        )
                    except Exception as exc:
                        logger.warning(f"WeComOCR LLM 修改意图解析失败：{exc}")
                        changes = {}
                if changes:
                    (
                        review.data,
                        review.cancellation_date_initialized,
                    ) = apply_review_changes(
                        review.data,
                        changes,
                        review.cancellation_date_initialized,
                    )
                    review.updated_at = time.monotonic()
                    review.error_count = 0
                    yield event.plain_result(
                        "已修改，请再次确认：\n" + format_fields(review.data)
                        + "\n\n继续修改请使用“字段改为值”；确认无误请回复“提交”。"
                    )
                    return
                yield event.plain_result(
                    self._review_error(
                        review, "该消息不是有效的信息修改、提交或退出指令。"
                    )
                )
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
                cached_path = await asyncio.to_thread(
                    self._file_cache.store, path, suffix
                )
                self._schedule_cache_deletion(cached_path)
                result = await asyncio.to_thread(self._ocr, cached_path)
                data = prepare_ocr_fields(result)
            except Exception as exc:
                logger.exception("WeComOCR OCR 处理失败：%s", exc)
                self._sessions.pop(key, None)
                yield event.plain_result("文件识别失败，本轮已清空。请确认文件格式和内容后重新上传；如仍失败请联系管理员。")
                return
            self._sessions[key] = ReviewSession(
                data=data,
                updated_at=time.monotonic(),
                cancellation_date_initialized=data["邮箱注销日期"] != "无",
            )
            yield event.plain_result(
                ignored_notice + "识别完成，请确认以下信息：\n" + format_fields(data)
                + "\n\n需要修改时回复“字段改为值”，多个修改用逗号或换行分隔；确认无误请回复“提交”。"
            )
            return

    async def terminate(self) -> None:
        tasks = list(self._cache_tasks)
        if self._cache_cleanup_task is not None:
            tasks.append(self._cache_cleanup_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._sessions.clear()
        self._locks.clear()
