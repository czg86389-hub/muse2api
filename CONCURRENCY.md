# Runtime concurrency

Use **账号池 → 任务调度** to choose manual concurrency or automatic capacity based on available accounts, capped by the configured limit. Changes apply without a restart; lowering the cap never cancels active work. Same-account leases remain exclusive and waiting slot admissions are FIFO. Unknown account health is eligible until a failure is confirmed.

Authenticated `GET /admin/scheduler` reports configuration, effective limit, active and waiting slots. `PUT /admin/scheduler` accepts `{ "mode": "auto", "manual_limit": 2, "auto_limit": 4 }`. Values are persisted in `data/scheduler.json`; manual and automatic caps support 1–32. The default preserves `MUSE2API_CONCURRENCY`. Set the automatic cap according to available CPU and memory.
