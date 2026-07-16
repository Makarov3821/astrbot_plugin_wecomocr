"""通过百度 AI Studio PaddleOCR-VL 识别单个图片或 PDF。

流程：提交一个本地文件或文件 URL，轮询任务，下载 JSONL，
最后输出一个 OCR 结果字典。API Key 不写入脚本。
"""

import argparse
import json
from html.parser import HTMLParser
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

try:
    import requests
except ImportError as exc:
    raise RuntimeError("缺少 requests，请先执行：pip install requests") from exc


TARGET_KEYS = ["姓名", "工号", "单位", "复旦邮箱", "离职日期"]
FIELD_ALIASES = {
    "姓名": ["姓名"],
    "工号": ["工号"],
    "单位": ["单位"],
    "复旦邮箱": ["复旦邮箱", "邮箱"],
    "离职日期": ["离职日期", "离校日期"],
}
FIELD_BOUNDARIES = sorted(
    {
        alias
        for aliases in FIELD_ALIASES.values()
        for alias in aliases
    } | {"性别", "填表日期", "停薪日期"},
    key=len,
    reverse=True,
)
KEEP_EMAIL_KEYWORDS = ["保留", "保留邮箱", "不注销"]

DEFAULT_JOB_URL = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
DEFAULT_MODEL = "PaddleOCR-VL-1.6"
DEFAULT_POLL_INTERVAL = 5.0
DEFAULT_JOB_TIMEOUT = 600
DEFAULT_REQUEST_TIMEOUT = 60
SUPPORTED_SUFFIXES = {".pdf", ".jpg", ".jpeg", ".png"}

OPTIONAL_PAYLOAD = {
    "useDocOrientationClassify": False,
    "useDocUnwarping": False,
    "useChartRecognition": False,
}


def debug_log(enabled: bool, message: str, **details: Any) -> None:
    """向 stderr 输出不包含 API Key 的实时调试信息。"""
    if not enabled:
        return
    suffix = " ".join(
        f"{key}={value!r}" for key, value in details.items()
    )
    if suffix:
        message = f"{message} | {suffix}"
    timestamp = time.strftime("%H:%M:%S")
    print(f"[DEBUG {timestamp}] {message}", file=sys.stderr, flush=True)


def normalize_date(date_str: str) -> str:
    """从混合文本中提取日期，并标准化为 YYYY.MM.DD。"""
    if not date_str or date_str == "无":
        return "无"
    match = re.search(
        r"(?<!\d)((?:19|20)\d{2})[年./-]\s*(\d{1,2})[月./-]\s*(\d{1,2})日?",
        date_str,
    )
    if not match:
        return "无"
    year, month, day = (int(part) for part in match.groups())
    return f"{year}.{month:02d}.{day:02d}"


def normalize_email(email: str) -> str:
    """保留邮箱用户名，并按业务规则统一为 fudan.edu.cn 域名。"""
    if not email or email == "无" or "@" not in email:
        return "无"
    local_part = email.split("@", 1)[0].strip()
    local_part = re.sub(r"[^a-zA-Z0-9._%+-]", "", local_part)
    return f"{local_part}@fudan.edu.cn" if local_part else "无"


def clean_value(value: str) -> str:
    value = value.replace("\n", " ")
    value = re.sub(r"\s+", " ", value)
    value = value.strip().strip("|：:=- ")
    value = re.sub(r"[*_#]", "", value).strip()
    return value or "无"


class _HTMLTableParser(HTMLParser):
    """提取百度 Markdown 中 HTML 表格的行与单元格文字。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell_parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "tr":
            self._row = []
        elif tag.lower() in {"td", "th"} and self._row is not None:
            self._cell_parts = []

    def handle_data(self, data: str) -> None:
        if self._cell_parts is not None:
            self._cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"} and self._cell_parts is not None:
            if self._row is not None:
                self._row.append(clean_value("".join(self._cell_parts)))
            self._cell_parts = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None


def extract_html_fields(ocr_text: str) -> dict[str, str]:
    """从 HTML 表格的相邻单元格中提取字段。"""
    parser = _HTMLTableParser()
    parser.feed(ocr_text)
    extracted: dict[str, str] = {}
    boundary_set = set(FIELD_BOUNDARIES)

    for row in parser.rows:
        for index, cell in enumerate(row[:-1]):
            for key, aliases in FIELD_ALIASES.items():
                if cell not in aliases or key in extracted:
                    continue
                candidate = row[index + 1]
                if candidate != "无" and candidate not in boundary_set:
                    extracted[key] = candidate
    return extracted


def _plain_text_without_tables(ocr_text: str) -> str:
    """移除 HTML 表格和其他标签，供普通标签文本解析。"""
    text_without_tables = re.sub(
        r"<table\b.*?</table>", "", ocr_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return re.sub(r"<[^>]+>", " ", text_without_tables)


def _next_boundary_position(text: str) -> int | None:
    positions = []
    for label in FIELD_BOUNDARIES:
        match = re.search(rf"{re.escape(label)}\s*[：:]", text)
        if match:
            positions.append(match.start())
    return min(positions) if positions else None


def extract_value(
    ocr_text: str,
    key: str,
    html_fields: dict[str, str] | None = None,
) -> str:
    """优先解析 HTML 单元格，再解析表格外的普通文本。"""
    html_fields = html_fields or extract_html_fields(ocr_text)
    if key in html_fields:
        return html_fields[key]

    aliases = FIELD_ALIASES[key]
    plain_text = _plain_text_without_tables(ocr_text)
    lines = [line.strip() for line in plain_text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        matches = [
            (line.find(alias), -len(alias), alias)
            for alias in aliases
            if line.find(alias) >= 0
        ]
        if not matches:
            continue
        _, _, matched_alias = min(matches)

        if "|" in line:
            cells = [clean_value(cell) for cell in line.split("|") if cell.strip()]
            for cell_index, cell in enumerate(cells):
                if matched_alias in cell and cell_index + 1 < len(cells):
                    candidate = cells[cell_index + 1]
                    if candidate != "无" and candidate not in FIELD_BOUNDARIES:
                        return candidate

        tail = line.split(matched_alias, 1)[1]
        boundary_position = _next_boundary_position(tail)
        if boundary_position is not None:
            tail = tail[:boundary_position]
        candidate = clean_value(tail)
        if candidate != "无":
            return candidate

        if index + 1 < len(lines):
            candidate = clean_value(lines[index + 1])
            if candidate != "无" and not any(
                label in candidate for label in FIELD_BOUNDARIES
            ):
                return candidate
    return "无"


def extract_information(ocr_text: str) -> tuple[dict[str, str], bool]:
    """提取目标字段，并执行工号、邮箱和日期清洗。"""
    html_fields = extract_html_fields(ocr_text)
    extracted_data = {
        key: extract_value(ocr_text, key, html_fields)
        for key in TARGET_KEYS
    }

    employee_id = extracted_data["工号"]
    if employee_id != "无":
        employee_id = employee_id.replace("∠", "L")
        employee_id = re.sub(r"[^a-zA-Z0-9]", "", employee_id)
        extracted_data["工号"] = employee_id or "无"

    extracted_data["复旦邮箱"] = normalize_email(extracted_data["复旦邮箱"])
    extracted_data["离职日期"] = normalize_date(extracted_data["离职日期"])

    keep_email = any(keyword in ocr_text for keyword in KEEP_EMAIL_KEYWORDS)
    return extracted_data, keep_email

def _response_json(response: requests.Response, action: str) -> dict[str, Any]:
    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        body = response.text[:2000]
        raise RuntimeError(
            f"{action}失败（HTTP {response.status_code}）：{body}"
        ) from exc
    try:
        payload = response.json()
    except requests.JSONDecodeError as exc:
        raise RuntimeError(f"{action}返回了无法解析的 JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{action}返回格式异常：{payload!r}")
    return payload


def submit_job(
    source: str,
    api_key: str,
    model: str,
    job_url: str,
    request_timeout: int,
    session: requests.Session,
    debug: bool = False,
) -> str:
    """上传本地文件或提交文件 URL，返回 jobId。"""
    headers = {"Authorization": f"bearer {api_key}"}
    debug_log(
        debug, "开始提交 OCR 任务", endpoint=job_url, model=model,
        source_type="URL" if source.startswith(("http://", "https://")) else "本地文件",
    )

    if source.startswith(("http://", "https://")):
        response = session.post(
            job_url,
            headers={**headers, "Content-Type": "application/json"},
            json={
                "fileUrl": source,
                "model": model,
                "optionalPayload": OPTIONAL_PAYLOAD,
            },
            timeout=request_timeout,
        )
    else:
        file_path = Path(source).expanduser().resolve()
        if not file_path.is_file():
            raise FileNotFoundError(f"输入文件不存在：{file_path}")
        if file_path.suffix.lower() not in SUPPORTED_SUFFIXES:
            supported = ", ".join(sorted(SUPPORTED_SUFFIXES))
            raise ValueError(
                f"不支持的文件类型 {file_path.suffix!r}，仅支持：{supported}"
            )
        debug_log(
            debug, "准备上传文件", file=file_path.name,
            size_bytes=file_path.stat().st_size,
        )
        with file_path.open("rb") as file_handle:
            response = session.post(
                job_url,
                headers=headers,
                data={
                    "model": model,
                    "optionalPayload": json.dumps(
                        OPTIONAL_PAYLOAD, ensure_ascii=False
                    ),
                },
                files={"file": (file_path.name, file_handle)},
                timeout=request_timeout,
            )

    payload = _response_json(response, "提交 OCR 任务")
    try:
        job_id = str(payload["data"]["jobId"])
        debug_log(debug, "OCR 任务提交成功", job_id=job_id)
        return job_id
    except (KeyError, TypeError) as exc:
        raise RuntimeError(f"提交响应缺少 jobId：{payload}") from exc


def wait_for_job(
    job_id: str,
    api_key: str,
    job_url: str,
    poll_interval: float,
    job_timeout: int,
    request_timeout: int,
    session: requests.Session,
    debug: bool = False,
) -> tuple[str, int]:
    """轮询 OCR 任务，返回结果 JSONL URL 和已提取页数。"""
    headers = {"Authorization": f"bearer {api_key}"}
    started_at = time.monotonic()
    deadline = started_at + job_timeout
    poll_count = 0
    status_url = f"{job_url.rstrip('/')}/{job_id}"

    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError(f"OCR 任务 {job_id} 在 {job_timeout} 秒内未完成")

        poll_count += 1
        debug_log(debug, "查询任务状态", poll=poll_count)
        payload = _response_json(
            session.get(
                status_url,
                headers=headers,
                timeout=request_timeout,
            ),
            "查询 OCR 任务",
        )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError(f"任务状态响应缺少 data：{payload}")

        state = data.get("state")
        progress = data.get("extractProgress") or {}
        debug_log(
            debug,
            "收到任务状态",
            poll=poll_count,
            state=state,
            elapsed_seconds=round(time.monotonic() - started_at, 1),
            extracted_pages=progress.get("extractedPages"),
            total_pages=progress.get("totalPages"),
        )
        if state == "done":
            try:
                result_url = data["resultUrl"]["jsonUrl"]
            except (KeyError, TypeError) as exc:
                raise RuntimeError(f"完成响应缺少 JSONL URL：{payload}") from exc
            pages = progress.get("extractedPages", 0)
            debug_log(debug, "OCR 任务完成", pages=pages)
            return str(result_url), int(pages or 0)

        if state == "failed":
            raise RuntimeError(
                f"OCR 任务失败：{data.get('errorMsg') or '未知错误'}"
            )
        if state not in {"pending", "running"}:
            raise RuntimeError(f"OCR 任务返回未知状态：{state!r}")

        time.sleep(poll_interval)


def download_results(
    jsonl_url: str,
    request_timeout: int,
    session: requests.Session,
    debug: bool = False,
) -> tuple[str, list[dict[str, Any]]]:
    """下载 JSONL，返回合并后的 Markdown 和页面结构化结果。"""
    result_host = urlparse(jsonl_url).netloc
    debug_log(debug, "开始下载 JSONL 结果", host=result_host)
    response = session.get(jsonl_url, timeout=request_timeout)
    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(
            f"下载 OCR 结果失败（HTTP {response.status_code}）："
            f"{response.text[:2000]}"
        ) from exc

    markdown_pages: list[str] = []
    structured_pages: list[dict[str, Any]] = []
    for line_number, line in enumerate(response.text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            document = json.loads(line)
            result = document["result"]
            pages = result["layoutParsingResults"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise RuntimeError(
                f"JSONL 第 {line_number} 行格式异常"
            ) from exc
        if not isinstance(pages, list):
            raise RuntimeError(
                f"JSONL 第 {line_number} 行的 layoutParsingResults 不是列表"
            )

        for page in pages:
            if not isinstance(page, dict):
                continue
            structured_pages.append(page)
            markdown = page.get("markdown") or {}
            if isinstance(markdown, dict):
                markdown_text = markdown.get("text")
            else:
                markdown_text = markdown
            if isinstance(markdown_text, str) and markdown_text.strip():
                markdown_pages.append(markdown_text.strip())

    if not markdown_pages:
        raise RuntimeError("百度 OCR 结果中没有可用的 Markdown 文本")
    debug_log(
        debug, "JSONL 结果解析完成", bytes=len(response.content),
        pages=len(structured_pages), markdown_pages=len(markdown_pages),
    )
    return "\n\n".join(markdown_pages), structured_pages


def _source_name(source: str) -> str:
    if source.startswith(("http://", "https://")):
        name = Path(unquote(urlparse(source).path)).name
        return name or source
    return Path(source).expanduser().name


def ocr_file(
    input_path: str | os.PathLike[str],
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    job_url: str = DEFAULT_JOB_URL,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    job_timeout: int = DEFAULT_JOB_TIMEOUT,
    request_timeout: int = DEFAULT_REQUEST_TIMEOUT,
    session: requests.Session | None = None,
    debug: bool = False,
) -> dict[str, Any]:
    """识别单个图片/PDF，并返回 OCR 结果字典。"""
    source = os.fspath(input_path)
    if poll_interval < 0:
        raise ValueError("poll_interval 不能小于 0")
    if job_timeout <= 0 or request_timeout <= 0:
        raise ValueError("超时时间必须大于 0")

    api_key = (
        api_key
        or os.getenv("PADDLEOCR_API_KEY")
        or os.getenv("PADDLEOCR_TOKEN")
    )
    if not api_key:
        raise ValueError(
            "缺少 API Key：请设置 PADDLEOCR_API_KEY 或传入 api_key"
        )

    debug_log(
        debug, "启动 OCR 流程", file=_source_name(source),
        model=model, job_timeout=job_timeout,
        request_timeout=request_timeout, poll_interval=poll_interval,
    )
    own_session = session is None
    session = session or requests.Session()
    try:
        job_id = submit_job(
            source, api_key, model, job_url, request_timeout, session, debug
        )
        jsonl_url, _reported_pages = wait_for_job(
            job_id,
            api_key,
            job_url,
            poll_interval,
            job_timeout,
            request_timeout,
            session,
            debug,
        )
        ocr_text, _structured_pages = download_results(
            jsonl_url, request_timeout, session, debug
        )
    finally:
        if own_session:
            session.close()

    extracted_data, keep_email = extract_information(ocr_text)

    return {
        "文件名": _source_name(source),
        "姓名": extracted_data["姓名"],
        "工号": extracted_data["工号"],
        "单位": extracted_data["单位"],
        "复旦邮箱": extracted_data["复旦邮箱"],
        "离职日期": extracted_data["离职日期"],
        "保留邮箱": "是" if keep_email else "否",
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="通过百度 AI Studio PaddleOCR-VL-1.6 识别图片或 PDF"
    )
    parser.add_argument("input", help="本地图片/PDF 路径或 HTTP(S) 文件 URL")
    parser.add_argument(
        "--api-key", default=None,
        help="API Key；默认读取 PADDLEOCR_API_KEY",
    )
    parser.add_argument(
        "--job-url",
        default=os.getenv("PADDLEOCR_JOB_URL", DEFAULT_JOB_URL),
    )
    parser.add_argument(
        "--model",
        default=os.getenv("PADDLEOCR_MODEL", DEFAULT_MODEL),
    )
    parser.add_argument(
        "--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL,
        help="轮询间隔秒数，默认 5",
    )
    parser.add_argument(
        "--job-timeout", type=int, default=DEFAULT_JOB_TIMEOUT,
        help="整个 OCR 任务超时秒数，默认 600",
    )
    parser.add_argument(
        "--request-timeout", type=int, default=DEFAULT_REQUEST_TIMEOUT,
        help="单次 HTTP 请求超时秒数，默认 60",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="向 stderr 实时输出任务提交、轮询和下载状态",
    )
    args = parser.parse_args()

    try:
        result = ocr_file(
            input_path=args.input,
            api_key=args.api_key,
            model=args.model,
            job_url=args.job_url,
            poll_interval=args.poll_interval,
            job_timeout=args.job_timeout,
            request_timeout=args.request_timeout,
            debug=args.debug,
        )
    except KeyboardInterrupt:
        print("\nOCR 任务已由用户取消。", file=sys.stderr, flush=True)
        raise SystemExit(130)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

