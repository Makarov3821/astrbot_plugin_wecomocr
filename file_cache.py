"""插件管理的上传文件临时缓存。"""

from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import time
from uuid import uuid4


class ManagedFileCache:
    def __init__(self, retention_hours: float = 1.0) -> None:
        self.retention_seconds = max(1.0, float(retention_hours)) * 3600
        self.root = (
            Path(tempfile.gettempdir()) / "astrbot_plugin_wecomocr_uploads"
        ).resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def store(self, source: str, suffix: str) -> str:
        source_path = Path(source).expanduser().resolve(strict=True)
        if not source_path.is_file():
            raise ValueError("上传附件不是可读取的普通文件")
        normalized_suffix = suffix.lower()
        if normalized_suffix not in {".jpg", ".jpeg", ".png", ".pdf"}:
            raise ValueError("不支持的缓存文件类型")
        target = self.root / f"{uuid4().hex}{normalized_suffix}"
        shutil.copyfile(source_path, target)
        target.chmod(0o600)
        return str(target)

    def _managed_path(self, value: str | Path) -> Path:
        path = Path(value).resolve()
        if path.parent != self.root:
            raise ValueError("拒绝删除插件缓存目录以外的文件")
        return path

    def delete(self, value: str | Path) -> None:
        path = self._managed_path(value)
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def seconds_until_expiry(self, value: str | Path) -> float:
        path = self._managed_path(value)
        try:
            created_at = path.stat().st_mtime
        except FileNotFoundError:
            return 0.0
        return max(0.0, created_at + self.retention_seconds - time.time())

    def cleanup_expired(self) -> int:
        cutoff = time.time() - self.retention_seconds
        deleted = 0
        for path in self.root.iterdir():
            if not path.is_file():
                continue
            try:
                if path.stat().st_mtime <= cutoff:
                    self.delete(path)
                    deleted += 1
            except FileNotFoundError:
                continue
        return deleted
