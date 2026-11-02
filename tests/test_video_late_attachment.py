"""Deterministic regression for successful media text preceding a late video.

Run directly. --probe PATH prints the same fixture outcomes for any engine source.
No account, network, browser, sleeps, or third-party test framework is required.
"""
import ast
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def load_waiter(path):
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    engine = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "MuseEngine")
    names = {"_wait_attachment", "_clean_model_text", "_media_refusal_text", "_media_status_pending"}
    methods = [n for n in engine.body if isinstance(n, ast.FunctionDef) and n.name in names]
    module = ast.Module(body=[
        ast.ClassDef(name="MuseGenerationError", bases=[ast.Name(id="RuntimeError", ctx=ast.Load())],
                     keywords=[], body=[ast.Pass()], decorator_list=[]),
        ast.ClassDef(name="MuseModelRefusalError", bases=[ast.Name(id="MuseGenerationError", ctx=ast.Load())],
                     keywords=[], body=[ast.Pass()], decorator_list=[]),
        ast.ClassDef(name="Waiter", bases=[], keywords=[], body=methods, decorator_list=[]),
    ], type_ignores=[])
    scope = {"re": re, "json": json}
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), scope)
    return scope


def scenario(source, text, ready_at=25, timeout=90, later_text=None, stop_until=0, cover=False):
    scope = load_waiter(source)
    clock = SimpleNamespace(now=0.0)
    def sleep(seconds):
        clock.now += seconds
    scope["time"] = SimpleNamespace(time=lambda: clock.now, sleep=sleep)
    waiter = scope["Waiter"]()
    def state(_script):
        current = later_text[1] if later_text and clock.now >= later_text[0] else text
        return json.dumps({"cnt": 1, "txt": current, "tail": current,
                           "stop": clock.now < stop_until, "path": "/thread/fixture"})
    waiter.page = SimpleNamespace(js=state)
    waiter._scroll_bottom = lambda: None
    def attachments():
        if ready_at is not None and clock.now >= ready_at:
            return [{"tid": "hatch-chat-attachment-presentation-video", "src": "blob:result",
                     "vSrc": "blob:result", "hasVideo": True, "w": 720, "h": 1280}]
        if cover:
            return [{"tid": "hatch-chat-attachment-presentation-video", "src": "blob:cover",
                     "vSrc": "", "hasVideo": False, "w": 720, "h": 1280}]
        return []
    waiter.attachments = attachments
    try:
        result = waiter._wait_attachment("", timeout, "video")
        outcome = "video" if result and result.get("vSrc") else "timeout"
        return {"outcome": outcome, "elapsed": round(clock.now, 1)}
    except scope["MuseGenerationError"] as exc:
        return {"outcome": type(exc).__name__, "elapsed": round(clock.now, 1), "error": str(exc)}


def cases(source):
    cn = "视频已生成，规格正好：720x1280 竖屏、时长 6 秒。 Video not found"
    en = "The video has been generated successfully. Video not found"
    result = {}
    for label, text in (("cn_claim", cn), ("cn_ready", "视频生成好了，时长6秒。"),
                        ("cn_short", "已生成，等待附件加载。"),
                        ("en_claim", en), ("en_ready", "Your video is ready."),
                        ("verified_report", "verified generated artifact")):
        for delay in (15, 30, 60):
            result[f"{label}_{delay}"] = scenario(source, text, ready_at=delay)
    result["claim_no_attachment"] = scenario(source, cn, ready_at=None, timeout=45)
    result["claim_only_cover"] = scenario(source, cn, ready_at=None, timeout=45, cover=True)
    result["stop_lifecycle"] = scenario(source, cn, ready_at=30, stop_until=20)
    refusal = "视频没生成成功：你这个账号所在的国家/地区目前不支持视频生成。"
    result["explicit_refusal"] = scenario(source, refusal)
    result["claim_then_refusal"] = scenario(source, cn, later_text=(12, refusal))
    for label, text in (
        ("negated_cn", "视频没有生成成功，请重新尝试。"),
        ("negated_en", "The video has not been generated successfully. Please try again."),
        ("negated_contraction", "Your video hasn't been generated. Please try again."),
        ("negated_could_not", "The video could not be generated."),
        ("negated_couldnt", "The video couldn't be generated."),
        ("negated_none", "No video has been generated."),
        ("claim_then_upload_failed", "The video was generated, but the upload failed."),
        ("failed_after_claim", "视频已生成，但随后处理失败，无法提供视频。"),
    ):
        result[label] = scenario(source, text)
    return result


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--probe":
        print(json.dumps(cases(sys.argv[2]), ensure_ascii=False, sort_keys=True))
        return
    source = ROOT / "engine.py"
    result = cases(source)
    for name, item in result.items():
        if name.startswith(("cn_claim_", "cn_ready_", "cn_short_", "en_claim_", "en_ready_", "verified_report_")):
            assert item["outcome"] == "video", (name, item)
            assert item["elapsed"] >= int(name.rsplit("_", 1)[1]), (name, item)
        elif name in ("claim_no_attachment", "claim_only_cover"):
            assert item["outcome"] == "timeout" and 45 <= item["elapsed"] < 46, (name, item)
        elif name == "stop_lifecycle":
            assert item["outcome"] == "video" and item["elapsed"] >= 30, (name, item)
        else:
            assert item["outcome"] == "MuseModelRefusalError", (name, item)
            assert item["elapsed"] < 25, (name, item)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    print(f"PASS: {len(result)} late-video, deadline, cover and refusal cases")


if __name__ == "__main__":
    main()
