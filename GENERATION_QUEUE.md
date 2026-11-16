# Independent queue and generation timeouts

Image/video generation budgets start only after acquiring both a concurrency slot and an account. Initial slot/account waits and retry-account waits consume the separate queue budget, not generation time.

The account-pool panel exposes the independent queue timeout. Authenticated `GET/PUT /admin/generation-queue` reads or saves `{ "queue_timeout": 3600 }` in `data/generation-queue.json`; `0` means unlimited waiting. A changed queue timeout applies to new jobs. Existing image/video generation-timeout settings remain unchanged.

Task records retain account, attempt, original error, queue duration, generation-only elapsed time and total elapsed time. An original upstream failure is not overwritten when the retry deadline expires. Old task records retain their historical duration display.

Video tasks are asynchronous. For images that may wait longer than an HTTP client timeout, use `/v1/images/tasks` and poll the existing task ID; do not resubmit merely because a polling request timed out.
