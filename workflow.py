"""离校信息确认与 WPS 提交的纯业务逻辑。"""

from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import urlparse

import requests

from .ocr_service import normalize_date, normalize_email


FIELDS = ("姓名", "工号", "单位", "复旦邮箱", "离职日期", "保留邮箱")
SUBMIT_PHRASES = {
    "提交",
    "确认提交",
    "确认并提交",
    "提交表格",
    "确认无误",
    "确认正确",
    "信息正确",
    "确认无误并提交",
    "信息正确并提交",
}


def is_submit_command(text: str) -> bool:
    normalized = re.sub(r"[\s。！!，,；;]+", "", text or "")
    return normalized in SUBMIT_PHRASES


def _normalize_manual_value(field: str, value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip().strip("。；;")
    if not value or len(value) > 200:
        raise ValueError(f"{field}的值为空或过长")

    if field == "离职日期":
        normalized = normalize_date(value)
        if normalized == "无" and value != "无":
            raise ValueError("离职日期请使用 YYYY.MM.DD、YYYY-MM-DD 或 YYYY年MM月DD日")
        return normalized
    if field == "复旦邮箱":
        normalized = normalize_email(value)
        if normalized == "无" and value != "无":
            raise ValueError("复旦邮箱需包含 @，或填写“无”")
        return normalized
    if field == "保留邮箱":
        if value in {"是", "保留", "需要", "需要保留"}:
            return "是"
        if value in {"否", "不保留", "不需要", "无需保留"}:
            return "否"
        raise ValueError("保留邮箱只能填写“是”或“否”")
    if field == "工号":
        if value == "无":
            return value
        normalized = re.sub(r"[^a-zA-Z0-9]", "", value.replace("∠", "L"))
        if not normalized:
            raise ValueError("工号只能包含字母和数字")
        return normalized
    return value


def parse_modifications(text: str) -> dict[str, str]:
    """严格解析一个或多个“字段改为值”，整条消息必须全部匹配。"""
    field_pattern = "|".join(map(re.escape, FIELDS))
    parts = re.split(
        rf"[\n；;]+|[，,](?=\s*(?:修改\s*)?(?:{field_pattern}))",
        (text or "").strip(),
    )
    changes: dict[str, str] = {}
    pattern = re.compile(
        rf"^\s*(?:修改\s*)?(?P<field>{field_pattern})\s*"
        rf"(?:改为|修改为|更改为|设置为|是|[:：])\s*(?P<value>.+?)\s*$"
    )
    for part in parts:
        if not part.strip():
            continue
        match = pattern.fullmatch(part)
        if not match:
            return {}
        field = match.group("field")
        changes[field] = _normalize_manual_value(field, match.group("value"))
    return changes


def select_fields(data: Mapping[str, Any]) -> dict[str, str]:
    return {field: str(data.get(field, "无") or "无") for field in FIELDS}


def format_fields(data: Mapping[str, Any]) -> str:
    selected = select_fields(data)
    return "\n".join(f"{field}：{selected[field]}" for field in FIELDS)


def validate_wps_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    if parsed.scheme != "https" or parsed.hostname != "365.kdocs.cn":
        raise ValueError("WPS 接口必须是 https://365.kdocs.cn 下的 AirScript 地址")
    if not parsed.path.endswith("/sync_task"):
        raise ValueError("WPS 接口必须以 /sync_task 结尾")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("WPS 接口地址不能包含凭据、查询参数或片段")
    return parsed.geturl()


def submit_to_wps(
    url: str,
    token: str,
    data: Mapping[str, Any],
    timeout: int = 30,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """仅向固定 WPS 域名提交六个白名单字段。"""
    endpoint = validate_wps_url(url)
    if not token:
        raise ValueError("缺少 AirScript-Token")
    if timeout <= 0:
        raise ValueError("WPS 请求超时必须大于 0")

    own_session = session is None
    client = session or requests.Session()
    try:
        response = client.post(
            endpoint,
            headers={
                "Content-Type": "application/json",
                "AirScript-Token": token,
            },
            json={"Context": {"argv": {"data": select_fields(data)}}},
            timeout=timeout,
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except requests.JSONDecodeError:
            payload = {"status_code": response.status_code}
        if isinstance(payload, dict):
            if payload.get("success") is False:
                raise RuntimeError("WPS AirScript 返回执行失败")
            error_code = payload.get("errorCode")
            if error_code not in (None, 0, "0", ""):
                raise RuntimeError(f"WPS AirScript 返回错误码：{error_code}")
        return payload if isinstance(payload, dict) else {"result": payload}
    except requests.RequestException as exc:
        status = getattr(exc.response, "status_code", None)
        suffix = f"（HTTP {status}）" if status else ""
        raise RuntimeError(f"WPS 提交失败{suffix}") from exc
    finally:
        if own_session:
            client.close()
