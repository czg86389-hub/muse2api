"""Live concurrency limits and independently budgeted generation queues."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import time
from collections import deque
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal


class SchedulerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["manual", "auto"] = "manual"
    manual_limit: int = Field(default=2, ge=1, le=32)
    auto_limit: int = Field(default=4, ge=1, le=32)


def account_available(account: dict) -> bool:
    """A renewable workspace cookie's age is not a login failure."""
    if not account.get("enabled", True) or not account.get("cookies"):
        return False
    if account.get("ok") is False:
        return False
    session_expiry = (account.get("cookies_exp") or {}).get("hatch_sess")
    if session_expiry and float(session_expiry) > 0 and float(session_expiry) <= time.time():
        return False
    return True


class SchedulerPolicy:
    def __init__(self, path: str, accounts, manual_limit: int = 2):
        self.path = Path(path)
        self.accounts = accounts
        self._lock = threading.RLock()
        self._settings = SchedulerSettings(manual_limit=min(32, max(1, manual_limit)))
        if self.path.exists():
            self._settings = SchedulerSettings.model_validate_json(self.path.read_text())

    def settings(self) -> dict:
        with self._lock:
            return self._settings.model_dump()

    def save(self, settings: SchedulerSettings):
        # Persist before publishing, so a failed write cannot silently change runtime policy.
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".scheduler-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(settings.model_dump_json(indent=2))
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
                self._settings = settings.model_copy(deep=True)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)

    def counts(self) -> dict:
        accounts = list(self.accounts())
        return {"available_accounts": sum(account_available(a) for a in accounts)}

    def limit(self) -> int:
        settings = self.settings()
        if settings["mode"] == "manual":
            return settings["manual_limit"]
        return min(settings["auto_limit"], self.counts()["available_accounts"])


class ConcurrencyLock:
    """FIFO admission; resizing never interrupts work or over-releases a semaphore."""
    def __init__(self, limit: int = 2, limit_provider=None):
        self.limit = max(1, limit)
        self.limit_provider = limit_provider
        self._cond = threading.Condition()
        self._active_count = 0
        self._waiters = deque()

    def effective_limit(self):
        return max(0, int(self.limit_provider() if self.limit_provider else self.limit))

    def notify(self):
        with self._cond:
            self._cond.notify_all()

    def snapshot(self):
        with self._cond:
            return {"effective_limit": self.effective_limit(), "active": self._active_count,
                    "waiting": len(self._waiters)}

    def acquire(self, blocking=True, timeout=-1):
        deadline = None if timeout is None or timeout < 0 else time.monotonic() + timeout
        with self._cond:
            if not blocking:
                if self._waiters or self._active_count >= self.effective_limit():
                    return False
                self._active_count += 1
                return True
            ticket = object()
            self._waiters.append(ticket)
            try:
                while True:
                    if self._waiters[0] is ticket and self._active_count < self.effective_limit():
                        self._active_count += 1
                        return True
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        return False
                    # Account imports/health changes take effect even without an explicit notification.
                    self._cond.wait(timeout=0.25 if remaining is None else min(0.25, remaining))
            finally:
                self._waiters.remove(ticket)
                self._cond.notify_all()

    def release(self):
        with self._cond:
            if self._active_count <= 0:
                raise RuntimeError("Concurrency slot released without an acquisition")
            self._active_count -= 1
            self._cond.notify_all()

    def locked(self):
        with self._cond:
            return self._active_count > 0

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_):
        self.release()


