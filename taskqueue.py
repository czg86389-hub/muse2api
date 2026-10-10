"""生成任务队列。

生产环境用 Redis 保存排队顺序。测试可把地址设为 memory://，
只用进程内的同等语义，不连接 Redis。
"""
from __future__ import annotations

import json
import threading

LIST_KEY = "muse2api:queue"
JOB_PREFIX = "muse2api:job:"

_ENQUEUE_LUA = """
local n = redis.call('LLEN', KEYS[1])
if n >= tonumber(ARGV[1]) then
  return -1
end
redis.call('RPUSH', KEYS[1], ARGV[2])
redis.call('SET', KEYS[2], ARGV[3])
return n + 1
"""


class QueueFull(RuntimeError):
    pass


class QueueUnavailable(RuntimeError):
    pass


class TaskQueue:
    def __init__(self, url: str):
        self.url = url or ""
        self._mem = self.url.startswith("memory:")
        self._lock = threading.Lock()
        self._ids: list[str] = []
        self._jobs: dict[str, str] = {}
        self._redis = None
        if not self._mem:
            import redis
            self._redis = redis.Redis.from_url(
                self.url, decode_responses=True,
                socket_connect_timeout=2, socket_timeout=3,
                health_check_interval=30)

    def ping(self) -> bool:
        if self._mem:
            return True
        try:
            return bool(self._redis.ping())
        except Exception:
            return False

    def enqueue(self, task_id: str, payload: dict, limit: int) -> int:
        """把任务放到队尾。返回 1 开始的位置。队列满时抛出 QueueFull。"""
        body = json.dumps(payload, ensure_ascii=False)
        if self._mem:
            with self._lock:
                if len(self._ids) >= limit:
                    raise QueueFull()
                self._ids.append(task_id)
                self._jobs[task_id] = body
                return len(self._ids)
        try:
            pos = self._redis.eval(
                _ENQUEUE_LUA, 2, LIST_KEY, JOB_PREFIX + task_id,
                int(limit), task_id, body)
        except Exception as exc:
            raise QueueUnavailable("任务队列不可用") from exc
        if int(pos) < 0:
            raise QueueFull()
        return int(pos)

    def pop(self) -> dict | None:
        """取出最早进入的任务。队列空时返回 None。"""
        if self._mem:
            with self._lock:
                if not self._ids:
                    return None
                task_id = self._ids.pop(0)
                raw = self._jobs.pop(task_id, "")
            return _decode(task_id, raw)
        try:
            task_id = self._redis.lpop(LIST_KEY)
            if not task_id:
                return None
            key = JOB_PREFIX + task_id
            raw = self._redis.get(key)
            self._redis.delete(key)
        except Exception as exc:
            raise QueueUnavailable("任务队列不可用") from exc
        return _decode(task_id, raw)

    def queued_ids(self) -> list[str]:
        if self._mem:
            with self._lock:
                return list(self._ids)
        try:
            return list(self._redis.lrange(LIST_KEY, 0, -1))
        except Exception as exc:
            raise QueueUnavailable("任务队列不可用") from exc

    def length(self) -> int:
        return len(self.queued_ids())

    def remove(self, task_id: str) -> None:
        if not task_id:
            return
        if self._mem:
            with self._lock:
                self._ids = [item for item in self._ids if item != task_id]
                self._jobs.pop(task_id, None)
            return
        try:
            self._redis.lrem(LIST_KEY, 0, task_id)
            self._redis.delete(JOB_PREFIX + task_id)
        except Exception as exc:
            raise QueueUnavailable("任务队列不可用") from exc

    def clear(self) -> None:
        if self._mem:
            with self._lock:
                self._ids.clear()
                self._jobs.clear()
            return
        try:
            ids = list(self._redis.lrange(LIST_KEY, 0, -1))
            pipe = self._redis.pipeline()
            pipe.delete(LIST_KEY)
            for task_id in ids:
                pipe.delete(JOB_PREFIX + task_id)
            pipe.execute()
        except Exception as exc:
            raise QueueUnavailable("任务队列不可用") from exc


def _decode(task_id: str, raw: str | None) -> dict:
    if not raw:
        return {"id": task_id, "missing": True}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {"id": task_id, "missing": True}
    if not isinstance(data, dict):
        return {"id": task_id, "missing": True}
    data["id"] = task_id
    return data
