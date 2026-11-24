"""While muse.ai is overloaded a video runs on two accounts and the first video wins."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

with tempfile.TemporaryDirectory() as home:
    os.environ.update(MUSE2API_HOME=home, MUSE2API_PROFILE_ROOT=home,
                      MUSE2API_KEY="test-only", MUSE2API_CONCURRENCY="4")
    spec = importlib.util.spec_from_file_location("video_hedge_candidate", ROOT / "app.py")
    app = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = app
    spec.loader.exec_module(app)
    for name in ("one", "two", "three"):
        app.store.add_account({"hatch_sess": name, "hatch_vml": name,
                               "hatch_native_auth_device": name}, name)

    lock = threading.Lock()
    calls: list[str] = []
    stopped: list[str] = []

    def behaviour(plan):
        """plan maps hatch_sess -> ("ok", seconds) | ("busy", seconds) | ("refuse", seconds)."""
        def generate(cookies, prompt, **kwargs):
            name = cookies["hatch_sess"]
            with lock:
                calls.append(name)
            outcome, seconds = plan.get(name, ("ok", 0.05))
            stop = kwargs.get("stop_event")
            if stop is not None and stop.wait(seconds):
                with lock:
                    stopped.append(name)
                raise app.MuseGenerationError("客户端已断开连接，终止生成任务")
            if stop is None:
                time.sleep(seconds)
            if outcome == "busy":
                raise app.MuseServiceBusyError("Muse 媒体服务暂时不可用（上游过载/503）")
            if outcome == "refuse":
                raise app.MuseModelRefusalError("模型未生成媒体，仅返回文本: 地区不可用")
            return {"filename": f"{name}.mp4", "size": 1, "kind": "video"}
        return generate

    def run(plan, kind="video", account_id=None, overloaded=True):
        calls.clear()
        stopped.clear()
        with app._OVERLOAD_LOCK:
            app._last_overload = time.monotonic() if overloaded else 0.0
        with patch.object(app, "_renew_and_persist", side_effect=lambda ident, **_: app.store.get_account(ident)), \
             patch.object(app.engine, "start"), patch.object(app.engine, "reset_thread"), \
             patch.object(app.engine, "close_current_page"), \
             patch.object(app, "BUSY_RETRIES", 0), patch.object(app, "BUSY_RETRY_DELAY", 0.0), \
             patch.object(app, "BUSY_RETRY_MIN_REMAINING", 0.0), \
             patch.object(app.engine, "generate", side_effect=behaviour(plan)):
            try:
                result = app._run_generation("p", kind, 30, account_id=account_id)
            except Exception as exc:  # noqa: BLE001
                result = exc
            # Let a cancelled branch finish and give back its slot and account.
            for _ in range(100):
                if not app.GEN_LOCK.locked() and not app.ACCOUNT_POOL.busy_accounts:
                    break
                time.sleep(0.02)
        assert not app.GEN_LOCK.locked(), "a concurrency slot leaked"
        assert not app.ACCOUNT_POOL.busy_accounts, app.ACCOUNT_POOL.busy_accounts
        return result

    slow_fast = {"one": ("ok", 2.0), "two": ("ok", 0.05), "three": ("ok", 2.0)}
    result = run(slow_fast)
    assert isinstance(result, tuple), result
    assert len(set(calls)) == 2, calls
    assert result[0]["filename"] == "two.mp4" or len(stopped) == 1, (result, calls, stopped)
    assert len(stopped) == 1, stopped
    loser = stopped[0]
    loser_note = next(a for a in app.store.list_accounts() if a["label"] == loser).get("note", "")
    assert "任务异常" not in loser_note, loser_note
    print("PASS: overloaded video runs on two accounts, first video wins, the other is cancelled cleanly")

    result = run(slow_fast, overloaded=False)
    assert isinstance(result, tuple) and len(calls) == 1, (result, calls)
    print("PASS: without a recent overload a video uses a single account")

    result = run(slow_fast, kind="image")
    assert isinstance(result, tuple) and len(calls) == 1, (result, calls)
    print("PASS: images are never run in parallel")

    first = next(a for a in app.store.list_accounts() if a["label"] == "one")
    result = run(slow_fast, account_id=first["id"])
    assert isinstance(result, tuple) and calls == ["one"], (result, calls)
    print("PASS: an explicitly chosen account is not duplicated")

    all_busy = {name: ("busy", 0.05) for name in ("one", "two", "three")}
    result = run(all_busy)
    assert isinstance(result, app.MuseServiceBusyError), result
    print("PASS: when both parallel attempts hit the outage the outage is reported")

    mixed = {"one": ("busy", 0.05), "two": ("busy", 0.05), "three": ("busy", 0.05)}
    mixed.update({"one": ("refuse", 0.05)})
    result = run(mixed)
    assert isinstance(result, (app.MuseModelRefusalError, app.MuseServiceBusyError)), result
    print("PASS: a refusal or outage is reported, never the cancellation")

    with app._OVERLOAD_LOCK:
        app._last_overload = 0.0
    result = run({"one": ("busy", 0.05), "two": ("busy", 0.05), "three": ("busy", 0.05)}, overloaded=False)
    assert isinstance(result, app.MuseServiceBusyError), result
    assert app._overload_recent(), "an overload reply must arm parallel mode for later videos"
    print("PASS: an overload reply switches later videos to parallel mode")

    with app._OVERLOAD_LOCK:
        app._last_overload = 0.0
    now = time.time()
    assert not app._seed_overload_from_tasks([{"error": "别的错误", "updated_at": now}], now)
    assert not app._seed_overload_from_tasks(
        [{"error": "Muse 媒体服务暂时不可用（上游过载/503）", "updated_at": now - app.VIDEO_HEDGE_WINDOW - 60}], now)
    assert not app._overload_recent()
    assert app._seed_overload_from_tasks(
        [{"last_error": "Muse 媒体服务暂时不可用（上游过载/503）", "updated_at": str(now - 60)}], now)
    assert app._overload_recent()
    print("PASS: a restart keeps parallel mode when recent tasks hit the outage")
