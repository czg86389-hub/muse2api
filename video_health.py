"""Temporary per-account video refusal cooldowns; no claim about provider policy."""
from __future__ import annotations
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from pydantic import BaseModel, ConfigDict, Field

class VideoHealthSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    video_cooldown: int = Field(default=900, ge=0, le=86400)

class VideoHealth:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.value = VideoHealthSettings.model_validate_json(self.path.read_text()) if self.path.exists() else VideoHealthSettings()

    def settings(self):
        with self.lock: return self.value.model_dump()

    def save(self, value):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".video-health-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(value.model_dump_json()); f.flush(); os.fsync(f.fileno())
                os.replace(tmp, self.path); self.value = value.model_copy(deep=True)
            finally:
                if os.path.exists(tmp): os.unlink(tmp)

    def eligible(self, account, kind="video"):
        if kind != "video": return True
        if not account.get("enabled", True) or not account.get("cookies") or account.get("ok") is False: return False
        expiry = (account.get("cookies_exp") or {}).get("hatch_sess") or 0
        if expiry > 0 and expiry <= time.time(): return False
        return self.settings()["video_cooldown"] == 0 or (account.get("video_cooldown_until") or 0) <= time.time()

    def refusal_updates(self, account, message):
        failures = int(account.get("video_refusal_count") or 0) + 1
        duration = self.settings()["video_cooldown"]
        explicit = bool(re.search(r"(?:国家|地区|country|region).*(?:不支持|不可用|限制|unavailable|unsupported|not available)|\b403\b", message, re.I))
        updates = {"video_refusal_count": failures, "video_last_error": message[:300]}
        if duration and (explicit or failures >= 2): updates["video_cooldown_until"] = time.time() + duration
        return updates

    @staticmethod
    def reset_updates():
        return {"video_refusal_count": 0, "video_cooldown_until": 0, "video_last_error": ""}

    def status(self, accounts):
        return {**self.settings(), "eligible_video_accounts": sum(self.eligible(a) for a in accounts),
                "cooling_accounts": [{"id":a["id"],"label":a.get("label") or a["id"],"until":a["video_cooldown_until"]}
                    for a in accounts if self.settings()["video_cooldown"] and (a.get("video_cooldown_until") or 0) > time.time()]}
