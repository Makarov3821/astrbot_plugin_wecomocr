"""离校信息确认与 WPS 提交的纯业务逻辑。"""

from __future__ import annotations

from datetime import datetime, timedelta
import re
from typing import Any, Mapping
from urllib.parse import urlparse

import requests

from .ocr_service import normalize_date, normalize_email


FIELDS = (
    "姓名",
    "工号",
    "单位",
    "复旦邮箱",
    "离职日期",
    "保留邮箱",
    "邮箱注销日期",
)
FIELD_LOOKUP = {field.casefold(): field for field in FIELDS}
EXIT_PHRASES = {
    "退出",
    "取消",
    "取消本轮",
    "结束",
    "结束本轮",
    "清空",
    "清空本轮",
    "重新开始",
    "重置",
    "exit",
    "cancel",
    "reset",
}
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
    "submit",
    "confirm",
}


def _normalize_command(text: str) -> str:
    return re.sub(r"[\s。！!，,；;]+", "", text or "").casefold()


def is_exit_command(text: str) -> bool:
    return _normalize_command(text) in EXIT_PHRASES


def is_submit_command(text: str) -> bool:
    return _normalize_command(text) in SUBMIT_PHRASES


def normalize_business_date(value: str) -> str:
    """标准化并验证真实日历日期。"""
    normalized = normalize_date(value)
    if normalized == "无":
        return "无"
    try:
        datetime.strptime(normalized, "%Y.%m.%d")
    except ValueError:
        return "无"
    return normalized


def calculate_email_cancellation_date(departure_date: str) -> str:
    """以离职日期为起点增加 31 个自然日。"""
    normalized = normalize_business_date(departure_date)
    if normalized == "无":
        return "无"
    parsed = datetime.strptime(normalized, "%Y.%m.%d")
    return (parsed + timedelta(days=31)).strftime("%Y.%m.%d")


def _normalize_manual_value(field: str, value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip().strip("。；;")
    if not value or len(value) > 200:
        raise ValueError(f"{field}的值为空或过长")

    if field in {"离职日期", "邮箱注销日期"}:
        normalized = normalize_business_date(value)
        if normalized == "无" and value != "无":
            raise ValueError(
                f"{field}请使用真实有效的 YYYY.MM.DD、YYYY-MM-DD 或 YYYY年MM月DD日"
            )
        return normalized
    if field == "复旦邮箱":
        normalized = normalize_email(value)
        if normalized == "无" and value != "无":
            raise ValueError("复旦邮箱需包含 @，或填写“无”")
        return normalized
    if field == "保留邮箱":
        if value not in {"是", "否"}:
            raise ValueError("保留邮箱只能填写“是”或“否”")
        return value
    if field == "工号":
        if value == "无":
            return value
        normalized = re.sub(r"[^a-zA-Z0-9]", "", value.replace("∠", "L"))
        if not normalized:
            raise ValueError("工号只能包含字母和数字")
        return normalized
    return value


def validate_modification_changes(
    changes: Mapping[str, Any],
) -> dict[str, str]:
    """校验 LLM 或其他结构化来源给出的修改。"""
    normalized: dict[str, str] = {}
    for raw_field, raw_value in changes.items():
        field = FIELD_LOOKUP.get(str(raw_field).strip().casefold())
        if field is None:
            raise ValueError(f"不允许修改字段：{raw_field}")
        normalized[field] = _normalize_manual_value(field, str(raw_value))
    return normalized


def parse_modifications(text: str) -> dict[str, str]:
    """严格解析一个或多个“字段改为值”，整条消息必须全部匹配。"""
    field_pattern = "|".join(map(re.escape, FIELDS))
    parts = re.split(
        rf"[\n；;]+|[，,](?=\s*(?:请\s*)?(?:把|将)?\s*(?:修改\s*)?(?:{field_pattern}))",
        (text or "").strip(),
    )
    changes: dict[str, str] = {}
    pattern = re.compile(
        rf"^\s*(?:请\s*)?(?:把|将)?\s*(?:修改\s*)?"
        rf"(?P<field>{field_pattern})\s*"
        rf"(?:改为|改成|换成|修改为|更改为|设置为|应为|应该为|应该是|是|[:：])"
        rf"\s*(?P<value>.+?)\s*$",
        re.IGNORECASE,
    )
    for part in parts:
        if not part.strip():
            continue
        match = pattern.fullmatch(part)
        if not match:
            return {}
        field = FIELD_LOOKUP[match.group("field").casefold()]
        changes[field] = _normalize_manual_value(field, match.group("value"))
    return changes


def select_fields(data: Mapping[str, Any]) -> dict[str, str]:
    selected = {field: str(data.get(field, "无") or "无") for field in FIELDS}
    selected["离职日期"] = normalize_business_date(selected["离职日期"])
    selected["邮箱注销日期"] = normalize_business_date(selected["邮箱注销日期"])
    if selected["保留邮箱"] not in {"是", "否"}:
        selected["保留邮箱"] = "否"
    return selected


def prepare_ocr_fields(data: Mapping[str, Any]) -> dict[str, str]:
    """生成供用户确认的七字段字典。"""
    selected = select_fields(data)
    selected["邮箱注销日期"] = calculate_email_cancellation_date(
        selected["离职日期"]
    )
    return selected


def apply_review_changes(
    data: Mapping[str, Any],
    changes: Mapping[str, str],
    cancellation_date_initialized: bool,
) -> tuple[dict[str, str], bool]:
    """合并用户修改，并且最多自动初始化一次邮箱注销日期。"""
    updated = select_fields(data)
    normalized_changes = dict(changes)
    if "邮箱注销日期" in normalized_changes:
        cancellation_date_initialized = True
    elif (
        "离职日期" in normalized_changes
        and not cancellation_date_initialized
    ):
        cancellation_date = calculate_email_cancellation_date(
            normalized_changes["离职日期"]
        )
        if cancellation_date != "无":
            normalized_changes["邮箱注销日期"] = cancellation_date
            cancellation_date_initialized = True
    updated.update(normalized_changes)
    return updated, cancellation_date_initialized


def format_fields(data: Mapping[str, Any]) -> str:
    selected = select_fields(data)
    return "\n".join(f"{field}：{selected[field]}" for field in FIELDS)


def build_wps_data(data: Mapping[str, Any]) -> dict[str, str]:
    """将七个确认字段映射为 WPS 数据表字段。"""
    raw_keep_email = str(data.get("保留邮箱", ""))
    if raw_keep_email not in {"是", "否"}:
        raise ValueError("保留邮箱只能是“是”或“否”")
    selected = select_fields(data)
    keep_email = selected["保留邮箱"]

    wps_data = {
        "姓名": selected["姓名"],
        "工号": selected["工号"],
        "原院系单位": selected["单位"],
        "复旦邮箱": selected["复旦邮箱"],
        "离职日期": selected["离职日期"],
    }
    if keep_email == "是":
        wps_data["保留姓名"] = selected["姓名"]
    else:
        cancellation_date = normalize_business_date(selected["邮箱注销日期"])
        if cancellation_date == "无":
            raise ValueError(
                "保留邮箱为“否”时，必须先填写真实有效的邮箱注销日期"
            )
        wps_data["邮箱注销日期"] = cancellation_date
    return wps_data


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
    """仅向固定 WPS 域名提交经过业务映射的字段。"""
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
            json={"Context": {"argv": {"data": build_wps_data(data)}}},
            timeout=timeout,
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except requests.JSONDecodeError:
            payload = {"status_code": response.status_code}
        if isinstance(payload, dict):
            error = payload.get("error")
            if error:
                raise RuntimeError(f"WPS AirScript 执行失败：{error}")
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
