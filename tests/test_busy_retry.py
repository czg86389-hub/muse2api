"""A temporary Muse media outage (overload / 503) is retried; a refusal still is not."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

with tempfile.TemporaryDirectory() as home:
    os.environ.update(MUSE2API_HOME=home, MUSE2API_PROFILE_ROOT=home,
                      MUSE2API_KEY="test-only")
    spec = importlib.util.spec_from_file_location("busy_retry_candidate", ROOT / "app.py")
    app = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = app
    spec.loader.exec_module(app)
    busy = app.MuseEngine._media_service_busy

    # Replies observed from muse.ai on 2026-11-24 while its video backend was overloaded.
    for text in (
        "没能生成出来，视频服务现在过载了，报了 503，不是规格的问题。",
        "这次没生成成功 — 视频生成服务暂时过载（503），没有产出视频文件。",
        "视频生成服务暂时不可用，这次没能生成出来。",
        "这次没能生成出来：视频生成服务那边返回了错误，重试 3 次后仍不可用，所以没有产出视频。",
        "抱歉，这次没能生成出来。 刚才尝试生成时，视频服务那边暂时不可用，试了几次都返回了错误。",
        "Sorry, the video service is overloaded right now (503). Please try again later.",
    ):
        assert busy(text), text
    for text in (
        "没生成成功：你所在的国家或地区暂不支持视频生成。",
        "这个内容违反了使用政策，我不能生成。",
        "账号额度已用完，暂时无法生成视频。",
        "我建议的 reconciled 版本： 格式：9:16 竖屏，720x1280",
        "",
    ):
        assert not busy(text), text
    print("PASS: overload/503 replies are classified as a temporary outage")


    first = app.store.add_account({"hatch_sess": "one", "hatch_vml": "one",
                                   "hatch_native_auth_device": "one"}, "one")
    app.store.add_account({"hatch_sess": "two", "hatch_vml": "two",
                           "hatch_native_auth_device": "two"}, "two")
    common = [
        patch.object(app, "_renew_and_persist", side_effect=lambda ident, **_: app.store.get_account(ident)),
        patch.object(app.engine, "start"), patch.object(app.engine, "reset_thread"),
        patch.object(app.engine, "close_current_page"),
        patch.object(app, "BUSY_RETRY_DELAY", 0.0), patch.object(app, "BUSY_RETRY_MIN_REMAINING", 0.0),
    ]

    def run(side_effect, account_id=None):
        calls = []

        def generate(cookies, prompt, **kwargs):
            calls.append(cookies["hatch_sess"])
            return side_effect(len(calls))

        for p in common:
            p.start()
        try:
            with patch.object(app.engine, "generate", side_effect=generate):
                try:
                    result = app._run_generation_locked("video", "video", 60, account_id=account_id,
                                                        deadline=app.time.monotonic() + 60)
                except Exception as exc:  # noqa: BLE001
                    return calls, exc
                return calls, result
        finally:
            for p in reversed(common):
                p.stop()

    outage = app.MuseServiceBusyError("Muse 媒体服务暂时不可用（上游过载/503）")

    def recover(n):
        if n == 1:
            raise outage
        return {"filename": "ok.mp4", "size": 1, "kind": "video"}

    calls, result = run(recover, account_id=first["id"])
    assert isinstance(result, tuple) and result[0]["filename"] == "ok.mp4", result
    assert len(calls) == 2, calls
    print("PASS: a request succeeds when the outage clears on the retry")

    def always_busy(n):
        raise outage

    calls, exc = run(always_busy, account_id=first["id"])
    assert isinstance(exc, app.MuseServiceBusyError), exc
    assert len(calls) == 2 + app.BUSY_RETRIES, calls
    print("PASS: a persistent outage stops after the bounded number of retries")

    def busy_then_timeout(n):
        if n == 1:
            raise outage
        raise app.MuseGenerationError("等待生成超时，未出现新的生成结果")

    calls, exc = run(busy_then_timeout, account_id=first["id"])
    assert exc is outage, exc
    print("PASS: a retry that times out still reports the outage")

    def refuse(n):
        raise app.MuseModelRefusalError("模型未生成媒体，仅返回文本: 地区不可用")

    calls, exc = run(refuse, account_id=first["id"])
    assert isinstance(exc, app.MuseModelRefusalError) and calls == ["one"], (exc, calls)
    print("PASS: a model refusal is still terminal")

    app.store.delete_account([a for a in app.store.list_accounts() if a["label"] == "two"][0]["id"])
    calls, result = run(recover)
    assert isinstance(result, tuple) and calls == ["one", "one"], (result, calls)
    print("PASS: with one account the outage is retried on the same account")
