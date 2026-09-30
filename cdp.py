"""极简 Chrome DevTools Protocol 客户端（仅依赖 websocket-client / requests）。

关键设计（2026-09-30 修复 CDP 永久挂起缺陷）：
  * **单一读循环线程**：后台线程持续 recv()，把带 id 的响应投递到 dict，
    其余事件交给 on_event 回调。业务线程只 wait 结果，绝不直接 recv()。
    ⇒ 彻底避免「事件积压导致 TCP 背压 → send() 阻塞」和
      「recv() 死等一个永不返回的响应」两个死锁源。
  * **写超时**：ws.send 在锁内执行，配合 socket 超时；失败即抛错，不再静默。
  * **连接健康判定**：读循环线程一旦异常退出，所有 pending 请求立即被失败唤醒，
    不再无限等待。
"""
from __future__ import annotations

import json
import threading
import time

import requests
import websocket


class CDPError(RuntimeError):
    pass


class CDPTimeout(TimeoutError):
    pass


class CDP:
    def __init__(self, ws_url: str, timeout: float = 90.0, max_size: int = 256 << 20):
        self.ws = websocket.create_connection(ws_url, timeout=timeout, max_size=max_size)
        self.timeout = timeout
        self._id = 0

        # ---- 读循环状态 ----
        self._lock = threading.Lock()          # 保护 _id / _pending / _send_lock
        self._send_lock = threading.Lock()     # 串行化 ws.send（websocket-client 非线程安全）
        self._pending: dict[int, dict] = {}    # id -> {"event": Event, "msg": msg|None, "err": str|None}
        self._on_event = None                  # 可选：事件回调
        self._closed = False
        self._reader_dead = False
        self._reader_err = ""
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name="cdp-reader")
        self._reader.start()

    # ---------------- 读循环 ----------------
    def _read_loop(self):
        """后台线程：持续 recv，按 id 投递响应，其余交给事件回调。

        这是整个修复的核心 —— 业务线程不再直接触碰 socket 读端，
        因此不会因为「事件积压」或「等一个不来的响应」而永久阻塞。
        """
        self.ws.settimeout(1.0)  # 短超时，便于定期检查 _closed
        try:
            while not self._closed:
                try:
                    raw = self.ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue          # 正常：只是这段时间没数据，继续等
                except Exception as exc:  # noqa: BLE001
                    if self._closed:
                        break
                    # 非超时异常 = 连接已坏，必须唤醒所有等待者
                    self._reader_err = f"{type(exc).__name__}: {exc}"
                    break
                if not raw:
                    if self._closed:
                        break
                    continue
                try:
                    msg = json.loads(raw)
                except Exception:  # noqa: BLE001
                    continue
                mid = msg.get("id")
                if mid is not None:
                    with self._lock:
                        slot = self._pending.pop(mid, None)
                    if slot is not None:
                        slot["msg"] = msg
                        slot["event"].set()
                        continue
                    # 无主的响应（超时后迟到）丢弃
                    continue
                # 纯事件
                cb = self._on_event
                if cb is not None:
                    try:
                        cb(msg)
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            # 读循环退出 ⇒ 连接不可用，唤醒所有等待者避免永久挂起
            self._reader_dead = True
            with self._lock:
                slots = list(self._pending.values())
                self._pending.clear()
            for slot in slots:
                slot["err"] = self._reader_err or "CDP 读循环已退出（连接中断）"
                slot["event"].set()

    # ---------------- 请求 ----------------
    def send(self, method: str, params: dict | None = None, timeout: float | None = None):
        """发一条 CDP 命令并等待响应。写与读都有超时保护。"""
        if self._reader_dead:
            raise CDPError(f"CDP 连接不可用：{self._reader_err or '读循环已退出'}")

        with self._lock:
            self._id += 1
            mid = self._id
            slot = {"event": threading.Event(), "msg": None, "err": None}
            self._pending[mid] = slot

        payload = json.dumps({"id": mid, "method": method, "params": params or {}})
        try:
            with self._send_lock:
                # 显式设置写超时：防止 TCP 背压导致 send() 永久阻塞
                self.ws.settimeout(max(5.0, min(self.timeout, 30.0)))
                self.ws.send(payload)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._pending.pop(mid, None)
            # 写失败 ⇒ 连接很可能已坏，标记并唤醒其他等待者
            self._reader_dead = True
            self._reader_err = f"send 失败: {type(exc).__name__}: {exc}"
            with self._lock:
                slots = list(self._pending.values())
                self._pending.clear()
            for s in slots:
                s["err"] = self._reader_err
                s["event"].set()
            raise CDPError(f"{method}: {self._reader_err}") from exc

        # 等待响应：wait 本身有超时，绝不无限阻塞
        wait_for = timeout or self.timeout
        if not slot["event"].wait(wait_for):
            with self._lock:
                self._pending.pop(mid, None)
            raise CDPTimeout(f"CDP {method} 超时（{wait_for}s）")
        if slot["err"]:
            raise CDPError(f"{method}: {slot['err']}")
        msg = slot["msg"]
        if msg is None:
            raise CDPError(f"{method}: 无响应")
        if "error" in msg:
            raise CDPError(f"{method}: {msg['error']}")
        return msg

    def js(self, expr: str, await_promise: bool = False, timeout: float | None = None):
        r = self.send("Runtime.evaluate",
                      {"expression": expr, "returnByValue": True,
                       "awaitPromise": await_promise}, timeout)
        res = r.get("result", {}).get("result", {})
        if "value" in res:
            return res["value"]
        return r.get("result")

    def set_event_handler(self, on_event):
        """注册事件回调（由读循环线程调用）。"""
        self._on_event = on_event

    def pump(self, seconds: float, on_event=None, sock_timeout: float = 2.0):
        """兼容旧接口：在 seconds 秒内等待（事件已由读循环自动处理）。"""
        end = time.time() + seconds
        while time.time() < end:
            time.sleep(min(0.1, max(0.0, end - time.time())))

    def is_alive(self) -> bool:
        return (not self._closed) and (not self._reader_dead) and self._reader.is_alive()

    def close(self):
        self._closed = True
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


def http_json(url: str, timeout: float = 5.0):
    return requests.get(url, timeout=timeout).json()
