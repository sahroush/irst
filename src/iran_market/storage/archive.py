"""Optional raw archive: local files now, S3-compatible later via same interface.

Layout: {root}/{provider}/{operation}/{YYYY}/{MM}/{DD}/{ins_code}.json.zst
"""

from __future__ import annotations

import abc
from datetime import date
from pathlib import Path

try:
    import zstandard as zstd
except ImportError:  # optional unless raw archive is enabled
    zstd = None

from ..logging_setup import get_logger

log = get_logger("archive")


class RawArchive(abc.ABC):
    @abc.abstractmethod
    def save(self, provider: str, operation: str, key: str, day: date, payload: bytes) -> str:
        """Persist raw payload; returns a URI/locator."""

    @abc.abstractmethod
    def exists(self, provider: str, operation: str, key: str, day: date) -> bool: ...


class LocalZstArchive(RawArchive):
    """Local filesystem, zstd-compressed JSON."""

    def __init__(self, root: str | Path) -> None:
        if zstd is None:
            raise RuntimeError("zstandard is required when RAW_ARCHIVE_ENABLED=1")
        self.root = Path(root)

    def _path(self, provider: str, operation: str, key: str, day: date) -> Path:
        return self.root / provider / operation / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}" / f"{key}.json.zst"

    def save(self, provider: str, operation: str, key: str, day: date, payload: bytes) -> str:
        path = self._path(provider, operation, key, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(zstd.ZstdCompressor(level=3).compress(payload))
        return str(path)

    def exists(self, provider: str, operation: str, key: str, day: date) -> bool:
        return self._path(provider, operation, key, day).exists()

    def load(self, provider: str, operation: str, key: str, day: date) -> bytes | None:
        path = self._path(provider, operation, key, day)
        if not path.exists():
            return None
        return zstd.ZstdDecompressor().decompress(path.read_bytes(), max_output_size=1 << 30)


class NullArchive(RawArchive):
    """No-op archive (RAW_ARCHIVE_ENABLED=0)."""

    def save(self, provider: str, operation: str, key: str, day: date, payload: bytes) -> str:
        return ""

    def exists(self, provider: str, operation: str, key: str, day: date) -> bool:
        return False


def open_archive(settings) -> RawArchive:
    if settings.raw_archive_enabled:
        return LocalZstArchive(settings.raw_archive_dir)
    return NullArchive()
