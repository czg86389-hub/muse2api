"""Small, on-demand video previews. Never send video bytes as a fallback."""
from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import tempfile
import threading

VIDEO_FORMATS = {".mp4": "mov", ".mov": "mov", ".webm": "matroska"}
_workers = threading.BoundedSemaphore(2)
_locks = [threading.Lock() for _ in range(32)]


class ThumbnailUnavailable(Exception):
    pass


def source_version(stat) -> str:
    return f"{stat.st_mtime_ns:x}-{stat.st_ctime_ns:x}-{stat.st_size:x}"


def _prefix(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()


def _lock(name: str):
    return _locks[int(_prefix(name)[:8], 16) % len(_locks)]


def clear_thumbnail(cache_dir: str, name: str):
    with _lock(name):
        for path in Path(cache_dir).glob(f"{_prefix(name)}-*.jpg"):
            path.unlink(missing_ok=True)


def video_thumbnail(media_dir: str, cache_dir: str, name: str) -> Path:
    if not name or "/" in name or "\\" in name or ".." in name:
        raise ValueError("Invalid media name")
    source = Path(media_dir).resolve() / name
    if source.is_symlink() or not source.is_file():
        raise FileNotFoundError(name)
    fmt = VIDEO_FORMATS.get(source.suffix.lower())
    if fmt is None:
        raise ValueError("Unsupported video format")
    with _lock(name):
        version = source_version(source.stat())
        cache = Path(cache_dir)
        target = cache / f"{_prefix(name)}-{version}.jpg"
        if target.is_file():
            return target
        cache.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with _workers:
                with tempfile.NamedTemporaryFile(dir=cache, suffix=".jpg", delete=False) as f:
                    temporary = Path(f.name)
                subprocess.run([
                    "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                    # Force a local video container, never a playlist or remote URL.
                    "-protocol_whitelist", "file", "-f", fmt, "-threads", "1",
                    "-i", str(source), "-map", "0:v:0", "-frames:v", "1",
                    "-an", "-sn", "-vf",
                    "scale=w='min(480,iw)':h='min(300,ih)':force_original_aspect_ratio=decrease",
                    "-threads", "1", "-q:v", "5", str(temporary),
                ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=20)
            if not temporary.stat().st_size or source_version(source.stat()) != version:
                raise ThumbnailUnavailable("Video changed or produced no frame")
            temporary.replace(target)
            for old in cache.glob(f"{_prefix(name)}-*.jpg"):
                if old != target:
                    old.unlink(missing_ok=True)
            return target
        except (OSError, subprocess.SubprocessError) as exc:
            raise ThumbnailUnavailable("Video preview unavailable") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
