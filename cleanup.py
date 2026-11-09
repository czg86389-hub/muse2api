"""Media retention, size limits and startup junk sweep.

The module has no app/engine imports so it can be tested with a small fake config.
Deletion rules are intentionally conservative:

* hidden files (including ``.gitkeep``) are never deleted or counted;
* files touched within ``GRACE_SECONDS`` are left alone so an in-flight download is
  not removed while it is being written;
* media named by a queued/processing task is protected by the caller;
* only ``context-*`` and ``muse2api-refs-*`` directories are treated as orphans.
"""
from __future__ import annotations

import os
import shutil
import threading
import time

GRACE_SECONDS = 300
_ERROR_LIMIT = 20

_LOCK = threading.Lock()
STATE: dict = {"running": False, "last_run": None}


def _visible_files(media_dir: str) -> list[tuple[str, str, os.stat_result]]:
    items: list[tuple[str, str, os.stat_result]] = []
    if not os.path.isdir(media_dir):
        return items
    with os.scandir(media_dir) as entries:
        for entry in entries:
            if entry.name.startswith("."):
                continue
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                items.append((entry.path, entry.name, entry.stat(follow_symlinks=False)))
            except OSError:
                continue
    return items


def scan_media(media_dir: str) -> dict:
    files = _visible_files(media_dir)
    return {"count": len(files), "bytes": sum(st.st_size for _, _, st in files)}


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, names in os.walk(path, onerror=lambda _e: None):
        for name in names:
            try:
                total += os.stat(os.path.join(root, name), follow_symlinks=False).st_size
            except OSError:
                continue
    return total


def _remove_dir(root: str, prefix: str) -> tuple[int, int, list[str]]:
    """Remove direct child directories starting with ``prefix``; return count/bytes/errors."""
    removed = freed = 0
    errors: list[str] = []
    if not os.path.isdir(root):
        return removed, freed, errors
    try:
        entries = list(os.scandir(root))
    except OSError as exc:
        return removed, freed, [f"{root}: {exc}"]
    for entry in entries:
        if not entry.name.startswith(prefix):
            continue
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
            size = _dir_size(entry.path)
            shutil.rmtree(entry.path, ignore_errors=False)
            removed += 1
            freed += size
        except OSError as exc:
            errors.append(f"{entry.name}: {exc}")
    return removed, freed, errors


def sweep_orphans(cfg, debug_retention_days: int = 7, now: float | None = None) -> dict:
    """Remove orphan browser download/reference directories and stale debug dumps.

    Safe to call before the browser starts. Never touches the profile directory
    itself, accounts, tasks, settings or the media library.
    """
    now = time.time() if now is None else now
    if not _LOCK.acquire(blocking=False):
        return {"status": "busy", "dirs_removed": 0, "debug_removed": 0,
                "freed": 0, "errors": []}
    try:
        dirs_removed = debug_removed = freed = 0
        errors: list[str] = []

        count, size, errs = _remove_dir(cfg.download_dir, "context-")
        dirs_removed += count
        freed += size
        errors.extend(errs)

        count, size, errs = _remove_dir(cfg.profile_dir, "muse2api-refs-")
        dirs_removed += count
        freed += size
        errors.extend(errs)

        cutoff = now - max(1, int(debug_retention_days)) * 86400
        data_dir = cfg.data_dir
        if os.path.isdir(data_dir):
            try:
                entries = list(os.scandir(data_dir))
            except OSError as exc:
                entries = []
                errors.append(f"{data_dir}: {exc}")
            for entry in entries:
                if not entry.name.startswith("debug-") or not entry.name.endswith(".json"):
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    st = entry.stat(follow_symlinks=False)
                    if st.st_mtime >= cutoff:
                        continue
                    os.remove(entry.path)
                    debug_removed += 1
                    freed += st.st_size
                except OSError as exc:
                    errors.append(f"{entry.name}: {exc}")
        return {"status": "ok", "dirs_removed": dirs_removed,
                "debug_removed": debug_removed, "freed": freed,
                "errors": errors[:_ERROR_LIMIT]}
    finally:
        _LOCK.release()


def run_media_cleanup(cfg, settings: dict, protected_names=(), reason: str = "schedule",
                      now: float | None = None, grace_seconds: int = GRACE_SECONDS,
                      force: bool = False) -> dict:
    """Delete old/oversized media. ``force`` lets a manual run ignore the enabled flag."""
    if not _LOCK.acquire(blocking=False):
        return {"status": "busy", "reason": reason}
    started = time.monotonic()
    now = time.time() if now is None else now
    STATE["running"] = True
    try:
        enabled = bool(settings.get("media_cleanup_enabled", True))
        if not enabled and not force:
            return {"status": "skipped", "reason": reason, "enabled": False,
                    "at": int(now), "scanned": 0, "deleted": 0, "freed": 0,
                    "errors": [], "remaining": scan_media(cfg.media_dir),
                    "duration_ms": 0}

        retention_days = max(0, int(settings.get("media_retention_days") or 0))
        max_gb = max(0.0, float(settings.get("media_max_gb") or 0))
        max_bytes = int(round(max_gb * 1024 ** 3))
        protected = {os.path.basename(str(name)) for name in protected_names if name}

        files = _visible_files(cfg.media_dir)
        scanned = len(files)
        deleted = freed = 0
        errors: list[str] = []
        cutoff = now - retention_days * 86400 if retention_days else None

        def removable(name: str, st: os.stat_result) -> bool:
            if name in protected:
                return False
            return now - st.st_mtime >= grace_seconds

        def remove(path: str, name: str, st: os.stat_result) -> None:
            nonlocal deleted, freed
            try:
                os.remove(path)
                deleted += 1
                freed += st.st_size
            except FileNotFoundError:
                pass
            except OSError as exc:
                errors.append(f"{name}: {exc}")

        if cutoff is not None:
            for path, name, st in files:
                if st.st_mtime < cutoff and removable(name, st):
                    remove(path, name, st)

        if max_bytes:
            remaining_files = _visible_files(cfg.media_dir)
            total = sum(st.st_size for _, _, st in remaining_files)
            if total > max_bytes:
                candidates = sorted(remaining_files, key=lambda item: (item[2].st_mtime, item[1]))
                for path, name, st in candidates:
                    if total <= max_bytes:
                        break
                    if not removable(name, st):
                        continue
                    remove(path, name, st)
                    total -= st.st_size

        result = {
            "status": "ok",
            "reason": reason,
            "enabled": enabled,
            "at": int(now),
            "scanned": scanned,
            "deleted": deleted,
            "freed": freed,
            "errors": errors[:_ERROR_LIMIT],
            "remaining": scan_media(cfg.media_dir),
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        STATE["last_run"] = result
        return result
    finally:
        STATE["running"] = False
        _LOCK.release()


def state_snapshot() -> dict:
    return {"running": bool(STATE["running"]), "last_run": STATE.get("last_run")}
