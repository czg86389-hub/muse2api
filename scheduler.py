"""muse2api 生成调度器。

背景（为什么要引入这一层）
------------------------------------------------
修复前的架构是：每个请求 `threading.Thread(worker).start()`，线程各自去抢一把
全局裸锁 `GEN_LOCK = threading.Lock()`。这带来四个已实测确认的问题：

* **缺陷 3**：浏览器只有 1 个，三条路径（chat / image / video）共用一把锁，
  任何长任务都会把其它请求全部阻塞（实测 chat 被 video 挡住 90.5s / 101.5s）。
* **缺陷 10**：`threading.Lock` **不保证 FIFO**。后到的线程可能抢先拿到锁，
  早到的线程无限饥饿（实测并发 3 个任务，第 2 个耗时 227.8s 是第 1 个的 2 倍）。
* **缺陷 4**：没有队列、没有超时、没有淘汰。请求提交后 HTTP 立刻返回 200，
  但实际可能永远排不上；也没有任何机制把「等太久」的任务判死。
* **缺陷 9**：任务状态与 API 返回值脱节，僵尸任务对外伪装成 92%。

本模块把「抢锁」换成 **单 worker 线程 + `queue.Queue`**：

* `queue.Queue` 天然 FIFO —— 先提交先执行，饥饿问题消失（修缺陷 10）。
* 只有 1 个 worker 持锁 —— 语义等价于原来的串行，但**顺序可控、可观测**（修缺陷 3）。
* 出队前检查 `deadline` —— 排队超时的任务直接判 `timeout`，不浪费浏览器时间（修缺陷 4）。
* 每个任务都有明确的生命周期状态 —— 修缺陷 9 的进度/状态语义。

设计要点
------------------------------------------------
* **兼容旧接口**：`run(fn)` 保留，凡是没走队列的调用点（如管理页的探活）
  仍旧直接拿锁执行，行为不变。
* **幂等**：`stop()` 可重复调用。
* **daemon 线程**：容器退出时不会挂住。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

log = logging.getLogger("muse2api.scheduler")


# 任务生命周期（对外暴露的 status 取值）
ST_QUEUED = "queued"          # 已入队，尚未拿到浏览器
ST_RUNNING = "processing"     # 正在跑（worker 已出队）
ST_DONE = "completed"
ST_FAILED = "failed"
ST_TIMEOUT = "timeout"        # 排队超时 / 执行超时


class QueueTimeout(Exception):
    """任务在队列里等太久，还没轮到就被判死。"""


@dataclass(order=True)
class _Job:
    seq: int
    enqueued_at: float = field(compare=False, default=0.0)
    deadline: float = field(compare=False, default=0.0)
    fn: Callable = field(compare=False, default=None)
    label: str = field(compare=False, default="")
    # 由 worker 填充
    started: bool = field(compare=False, default=False)
    started_at: float = field(compare=False, default=0.0)
    finished: bool = field(compare=False, default=False)
    error: Optional[BaseException] = field(compare=False, default=None)
    # 同步调用（run_sync）时由 worker 回填 fn 的返回值；
    # 异步 submit 的调用方不取用，保持 None。
    result_value: Any = field(compare=False, default=None)
    wait_event: threading.Event = field(compare=False, default_factory=threading.Event)

    def __post_init__(self):
        # dataclass(order=True) 会用 seq 排序；但 field 默认值需手动补齐语义
        pass


class Scheduler:
    """单 worker 的 FIFO 生成调度器。

    Parameters
    ----------
    lock : threading.Lock
        真正保护浏览器的那把锁（原 GEN_LOCK）。worker 持锁执行任务；
        未走队列的旁路调用也可以直接 `with scheduler.lock:` 兼容旧行为。
    max_queue : int
        队列上限（防止内存被无限撑爆）。超出时 `submit` 抛 `QueueFull`。
    queue_timeout : int
        默认排队超时秒数。任务在队列里等待超过该值即判 `timeout`。
    """

    def __init__(self, lock: threading.Lock, max_queue: int = 100,
                 queue_timeout: int = 900, run_timeout: int = 0):
        self.lock = lock
        self.max_queue = max_queue
        self.queue_timeout = queue_timeout
        # run_timeout > 0 时，启动一个看门狗线程：单个 job 执行超过该秒数
        # 即强制 `engine.stop()`（由 watchdog 回调触发），把卡死的浏览器杀掉，
        # 从而释放锁。这是缺陷 4「无执行超时」的最后一道兜底。
        self.run_timeout = run_timeout
        self._q: "queue.Queue[_Job]" = queue.Queue(maxsize=max_queue)
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._running_job: Optional[_Job] = None
        self._running_lock = threading.Lock()
        self.on_run_timeout: Optional[Callable[[_Job], None]] = None
        # 观测指标
        self.metrics = {
            "submitted": 0,
            "completed": 0,
            "failed": 0,
            "timeout": 0,
            "rejected": 0,     # 队列满被拒
            "run_timeout": 0,  # 执行超时被看门狗中断
        }
        self._metrics_lock = threading.Lock()

    # ---------------- 生命周期 ----------------
    def start(self):
        if self._worker and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._loop, name="gen-worker",
                                        daemon=True)
        self._worker.start()
        log.info("【生成调度器】单 worker 已启动，队列上限=%d，排队超时=%ds",
                 self.max_queue, self.queue_timeout)

    def stop(self):
        self._stop.set()
        try:
            self._q.put_nowait(None)  # type: ignore[arg-type]  # 唤醒 worker
        except queue.Full:
            pass

    # ---------------- 对外 API ----------------
    @property
    def queue_size(self) -> int:
        return self._q.qsize()

    @property
    def running(self) -> Optional[dict]:
        with self._running_lock:
            j = self._running_job
            if not j:
                return None
            return {"label": j.label, "started_at": j.started_at,
                    "elapsed": round(time.time() - j.started_at, 1),
                    "enqueued_at": j.enqueued_at,
                    "waited": round(j.started_at - j.enqueued_at, 1)}

    def stats(self) -> dict:
        with self._metrics_lock:
            m = dict(self.metrics)
        r = self.running
        m.update({
            "queue_size": self.queue_size,
            "worker_alive": bool(self._worker and self._worker.is_alive()),
            "running": r,
        })
        return m

    def submit(self, fn: Callable, label: str = "",
               queue_timeout: int | None = None) -> _Job:
        """把任务 fn 放进 FIFO 队列。

        fn 会由唯一 worker 在线程里执行，执行期间持有 `self.lock`。
        fn 的返回值/异常不回传给调用方（异步任务语义）；调用方若需要
        结果，应在自己闭包里写 store。

        Raises
        ------
        queue.Full : 队列已满。
        """
        qto = int(queue_timeout if queue_timeout is not None else self.queue_timeout)
        with self._seq_lock:
            self._seq += 1
            seq = self._seq
        now = time.time()
        job = _Job(seq=seq, enqueued_at=now,
                   deadline=(now + qto) if qto > 0 else 0.0,
                   fn=fn, label=label or f"job#{seq}")
        try:
            self._q.put_nowait(job)
        except queue.Full:
            with self._metrics_lock:
                self.metrics["rejected"] += 1
            raise
        with self._metrics_lock:
            self.metrics["submitted"] += 1
        log.info("【生成调度器】任务入队 %s（队内=%d）", job.label, self.queue_size)
        return job

    # ---------------- worker ----------------
    def _loop(self):
        while not self._stop.is_set():
            try:
                job = self._q.get(timeout=1.0)
            except queue.Empty:
                continue
            if job is None:  # 停止信号
                break
            # 出队前检查是否已排队超时
            if job.deadline and time.time() > job.deadline:
                job.finished = True
                job.error = QueueTimeout(
                    f"任务排队超时（>{int(job.deadline - job.enqueued_at)}s 未轮到执行）")
                with self._metrics_lock:
                    self.metrics["timeout"] += 1
                log.warning("【生成调度器】任务排队超时被丢弃 %s（等了 %.1fs）",
                            job.label, time.time() - job.enqueued_at)
                job.wait_event.set()
                continue
            job.started = True
            job.started_at = time.time()
            with self._running_lock:
                self._running_job = job
            log.info("【生成调度器】开始执行 %s（排队 %.1fs）",
                     job.label, job.started_at - job.enqueued_at)
            wd = self._start_watchdog(job)
            try:
                with self.lock:
                    job.result_value = job.fn()
            except BaseException as exc:  # noqa: BLE001
                job.error = exc
                log.warning("【生成调度器】任务异常 %s: %s", job.label, exc)
                log.debug("%s", traceback.format_exc())
                with self._metrics_lock:
                    self.metrics["failed"] += 1
            else:
                with self._metrics_lock:
                    self.metrics["completed"] += 1
            finally:
                if wd is not None:
                    wd.cancel()
                job.finished = True
                job.wait_event.set()
                with self._running_lock:
                    self._running_job = None
                log.info("【生成调度器】执行结束 %s（耗时 %.1fs）",
                         job.label, time.time() - job.started_at)

    def _start_watchdog(self, job: _Job) -> Optional[threading.Timer]:
        """启动执行超时看门狗。超时后触发 on_run_timeout 回调（通常是杀浏览器）。

        注意：Timer 只是**触发**手段，真正让 job 结束仍依赖回调（engine.stop()）
        把卡住的 CDP/页面弄坏，进而让 job.fn() 抛错返回。
        """
        if self.run_timeout <= 0:
            return None

        def _fire():
            j = self._running_job
            if j is not job or job.finished:
                return
            log.error("【生成调度器】任务执行超时（>%ds）%s，触发看门狗强制中止",
                      self.run_timeout, job.label)
            with self._metrics_lock:
                self.metrics["run_timeout"] += 1
            cb = self.on_run_timeout
            if cb is not None:
                try:
                    cb(job)
                except Exception as exc:  # noqa: BLE001
                    log.warning("【生成调度器】看门狗回调异常: %s", exc)

        t = threading.Timer(self.run_timeout, _fire)
        t.daemon = True
        t.start()
        return t

    # ---------------- 兼容旧调用点 ----------------
    def run(self, fn: Callable) -> Any:
        """同步执行 fn（持有锁）。用于管理页探活等旁路调用。

        ⚠️ 注意：本方法**走旁路直接抢锁**，不经过 FIFO 队列。
        仅适用于低频管理操作（探活 / 读额度 / 预热）；生成类任务请用
        `submit`（异步）或 `run_sync`（同步排队）以确保不插队。
        """
        with self.lock:
            return fn()

    def run_sync(self, fn: Callable, label: str = "",
                 timeout: float | None = None,
                 queue_timeout: int | None = None) -> Any:
        """提交 fn 进 FIFO 队列，**阻塞**直到执行完成并返回其返回值。

        用于「已经在独立线程里、无法 await」的调用方（例如
        `app._queue_image` 的 worker 线程）。与 async 版 `_sched_run` 语义一致：
        走同一条队列，由唯一 worker 持锁执行，因此不会与 chat/video 抢浏览器。

        Parameters
        ----------
        fn : Callable
            要执行的任务，执行期间持有 ``self.lock``。
        label : str
            任务标签，仅用于日志/观测。
        timeout : float | None
            本方法**等待结果**的上限秒数。注意这不改变任务在 worker 里的
            执行超时（那由 ``run_timeout`` 看门狗负责）；它只是给调用方一个
            「最多等多久」的保护，超时则抛 ``QueueTimeout``。
        queue_timeout : int | None
            覆盖默认排队超时。

        Raises
        ------
        QueueTimeout
            排队阶段被判超时（worker 出队前检查），或调用方等待超时。
        异常透传
            fn 自身抛出的异常会原样再抛给调用方。
        """
        job = self.submit(fn, label=label, queue_timeout=queue_timeout)
        if timeout is None or timeout <= 0:
            job.wait_event.wait()
        elif not job.wait_event.wait(timeout):
            raise QueueTimeout(
                f"等待任务 {job.label} 结果超时（>{int(timeout)}s）")
        if job.error is not None:
            raise job.error
        return job.result_value


_scheduler: Optional[Scheduler] = None


def get_scheduler(lock: threading.Lock, **kw) -> Scheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = Scheduler(lock, **kw)
    return _scheduler
