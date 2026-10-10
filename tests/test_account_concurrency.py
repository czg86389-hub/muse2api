"""一个账号同时一个任务，多个账号可以重叠执行。"""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent


def load(home: str):
    os.environ.update(MUSE2API_HOME=home, MUSE2API_PROFILE_ROOT=home,
                      MUSE2API_KEY="test-only", MUSE2API_PUBLIC_BASE="",
                      REDIS_URL="memory://", MUSE2API_REDIS_URL="memory://")
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("account_concurrency_app", ROOT / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def add_account(module, label: str):
    return module.store.add_account(
        {"hatch_sess": label, "hatch_vml": label, "hatch_native_auth_device": label},
        label)


def main():
    with tempfile.TemporaryDirectory(prefix="muse-concurrency-") as home:
        module = load(home)
        first = add_account(module, "a")
        second = add_account(module, "b")

        # 两个线程各自看到自己的页面，不会串号。
        engine = module.MuseEngine(SimpleNamespace(
            profile_dir=home, login_wait=1, home_dir=home, data_dir=home,
            extra_path="", chromium="chromium", cdp_port=1, download_dir=home,
            site_url="https://muse.ai"))
        ready = threading.Barrier(2)
        seen = {}

        def bind(key, token):
            engine._save_page(key, token)
            engine._bind(key)
            ready.wait(2)
            seen[key] = engine.page

        threads = [
            threading.Thread(target=bind, args=(first["id"], "page-a")),
            threading.Thread(target=bind, args=(second["id"], "page-b")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
        assert seen[first["id"]] == "page-a"
        assert seen[second["id"]] == "page-b"
        print("thread_local_pages=PASS")

        # 两个账号的任务必须重叠，不能排成一队。
        overlap = threading.Barrier(2)

        def overlap_impl(prompt, kind, timeout, account_id=None, **kwargs):
            overlap.wait(2)
            return {"fixture": True}, account_id

        module._run_generation_locked = overlap_impl
        results = []

        def run():
            results.append(module._run_generation("p", "image", 3))

        workers = [threading.Thread(target=run), threading.Thread(target=run)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(4)
        assert not any(worker.is_alive() for worker in workers), "two accounts did not overlap"
        assert {item[1] for item in results} == {first["id"], second["id"]}
        assert not module.ACCOUNT_GATE.busy_ids()
        print("two_accounts_overlap=PASS")

        # 同一个账号第二次要等第一次结束。
        started = threading.Event()
        release = threading.Event()
        order = []

        def serial_impl(prompt, kind, timeout, account_id=None, **kwargs):
            order.append("start")
            if len(order) == 1:
                started.set()
                assert release.wait(2)
            order.append("end")
            return {"fixture": True}, account_id

        module.store.accounts[:] = [first]
        module._run_generation_locked = serial_impl
        workers = [threading.Thread(target=run), threading.Thread(target=run)]
        for worker in workers:
            worker.start()
        assert started.wait(1)
        time.sleep(0.2)
        assert order == ["start"], order
        release.set()
        for worker in workers:
            worker.join(3)
        assert order == ["start", "end", "start", "end"], order
        print("one_account_serial=PASS")

        # 一个账号失败后立刻换另一个账号，不等待，也不停掉整个浏览器。
        module.store.accounts[:] = [first, second]
        calls = []
        dropped = []

        def failover_impl(prompt, kind, timeout, account_id=None, **kwargs):
            calls.append(account_id)
            if len(calls) == 1:
                raise module.MuseGenerationError("fixture failed")
            return {"fixture": True}, account_id

        module._run_generation_locked = failover_impl
        module.engine.drop_session = lambda aid: dropped.append(aid)
        _res, used = module._run_generation("p", "image", 3)
        assert calls[0] != calls[1]
        assert used == calls[1]
        assert dropped == []
        assert not module.ACCOUNT_GATE.busy_ids()
        print("failover_other_account=PASS")

        # 文本回复不是可重试的生成失败。
        def text_only(prompt, kind, timeout, account_id=None, **kwargs):
            raise module.MuseGenerationError("未产出媒体附件，仅返回了文本回复")

        module._run_generation_locked = text_only
        try:
            module._run_generation("p", "image", 3)
            raise AssertionError("text-only reply must not be retried")
        except module.MuseGenerationError as exc:
            assert "仅返回了文本回复" in str(exc)
        assert not module.ACCOUNT_GATE.busy_ids()
        print("text_only_no_retry=PASS")

        # 排队时间不占用生成时限。
        module.store.accounts[:] = [first]
        assert module.ACCOUNT_GATE.try_acquire(first["id"])
        seen = {}

        def budget_impl(prompt, kind, timeout, account_id=None, **kwargs):
            seen["left"] = kwargs["deadline"] - time.monotonic()
            return {"fixture": True}, account_id

        module._run_generation_locked = budget_impl

        def delayed_release():
            time.sleep(0.4)
            module.ACCOUNT_GATE.release(first["id"])

        threading.Thread(target=delayed_release, daemon=True).start()
        module._run_generation("p", "image", 2)
        assert seen["left"] >= 1.8, seen
        assert not module.ACCOUNT_GATE.busy_ids()
        print("wait_does_not_shrink_budget=PASS")

        # 账号都在忙时，等待超过时限就失败，不把浏览器停掉。
        module.store.accounts[:] = [first, second]
        stopped = []
        module.engine.stop = lambda: stopped.append(True)
        assert module.ACCOUNT_GATE.try_acquire(first["id"])
        assert module.ACCOUNT_GATE.try_acquire(second["id"])
        try:
            module._run_generation("queue", "image", 1)
            raise AssertionError("busy accounts must time out")
        except module.MuseGenerationError as exc:
            assert "没有空闲账号" in str(exc)
        finally:
            module.ACCOUNT_GATE.release(first["id"])
            module.ACCOUNT_GATE.release(second["id"])
        assert stopped == []
        print("all_busy_times_out=PASS")
    print("PASS account concurrency")


if __name__ == "__main__":
    main()
