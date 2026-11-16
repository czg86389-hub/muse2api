"""Independent queue budget and generation task timing."""
from __future__ import annotations
import os
from pathlib import Path
import tempfile
import threading
import time
from pydantic import BaseModel, ConfigDict, Field

class QueueSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    queue_timeout: int = Field(default=3600, ge=0, le=86400)

class QueuePolicy:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.value = QueueSettings.model_validate_json(self.path.read_text()) if self.path.exists() else QueueSettings()

    def settings(self):
        with self.lock:
            return self.value.model_dump()

    def save(self, value):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".queue-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(value.model_dump_json()); f.flush(); os.fsync(f.fileno())
                os.replace(tmp, self.path)
                self.value = value.model_copy(deep=True)
            finally:
                if os.path.exists(tmp): os.unlink(tmp)

class TaskTiming:
    """Store queue, generation and total durations with consistent meanings."""
    def __init__(self, store, task_id):
        self.store, self.task_id = store, task_id
        self.created = time.monotonic()
        self.queue_elapsed = 0.0
        self.started = False

    def queued(self, seconds):
        self.queue_elapsed = seconds
        self.store.update_task(self.task_id, queue_elapsed=round(seconds, 1))

    def acquired(self):
        self.started = True
        self.store.update_task(self.task_id, status="processing", started_at=time.time())

    def account(self, aid, attempt):
        self.store.update_task(self.task_id, account=aid, attempt=attempt)

    def error(self, message):
        self.store.update_task(self.task_id, last_error=message)

    def finish(self):
        total = time.monotonic() - self.created
        return {"elapsed": round(max(0, total - self.queue_elapsed), 1) if self.started else 0,
                "queue_elapsed": round(self.queue_elapsed, 1), "total_elapsed": round(total, 1)}
