"""AstrBot 插件使用的 PaddleOCR 服务。"""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


_SOURCE = Path(__file__).with_name("ver-1.py")
_SPEC = spec_from_file_location("wecomocr_ver1", _SOURCE)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"无法加载 OCR 实现：{_SOURCE}")
_MODULE = module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

DEFAULT_JOB_URL = _MODULE.DEFAULT_JOB_URL
DEFAULT_MODEL = _MODULE.DEFAULT_MODEL
SUPPORTED_SUFFIXES = _MODULE.SUPPORTED_SUFFIXES
normalize_date = _MODULE.normalize_date
normalize_email = _MODULE.normalize_email
ocr_file = _MODULE.ocr_file

__all__ = [
    "DEFAULT_JOB_URL",
    "DEFAULT_MODEL",
    "SUPPORTED_SUFFIXES",
    "normalize_date",
    "normalize_email",
    "ocr_file",
]
