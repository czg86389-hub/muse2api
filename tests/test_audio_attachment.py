"""Deterministic audio attachment selection for _wait_attachment. No browser."""
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


def audio_card():
    return {"tid": "hatch-chat-attachment-presentation-audio", "src": "",
            "vSrc": "", "iSrc": "", "aSrc": "", "hasVideo": False,
            "hasImg": False, "hasAudio": False, "isAudio": True, "w": 0, "h": 0}


def video_card():
    return {"tid": "hatch-chat-attachment-presentation-video", "src": "blob:video",
            "vSrc": "blob:video", "iSrc": "", "aSrc": "", "hasVideo": True,
            "hasImg": False, "hasAudio": False, "isAudio": False, "w": 720, "h": 1280}


def image_card():
    return {"tid": "hatch-chat-attachment-presentation-image", "src": "blob:image",
            "vSrc": "", "iSrc": "blob:image", "aSrc": "", "hasVideo": False,
            "hasImg": True, "hasAudio": False, "isAudio": False, "w": 1024, "h": 1024}


def scenario(source, cards_at=None, timeout=45, text="音频已生成，文件 voice.mp3",
             stop_until=0, cards=None):
    scope = load_waiter(source)
    clock = SimpleNamespace(now=0.0)

    def sleep(seconds):
        clock.now += seconds

    scope["time"] = SimpleNamespace(time=lambda: clock.now, sleep=sleep)
    waiter = scope["Waiter"]()

    def state(_script):
        return json.dumps({"cnt": 1, "txt": text, "tail": text,
                           "stop": clock.now < stop_until, "path": "/thread/fixture"})

    waiter.page = SimpleNamespace(js=state)
    waiter._scroll_bottom = lambda: None
    waiter._composer_unsent = lambda: ""
    waiter._click_send = lambda: None

    def attachments():
        if cards is not None:
            return list(cards)
        if cards_at is not None and clock.now >= cards_at:
            return [audio_card()]
        return []

    waiter.attachments = attachments
    try:
        result = waiter._wait_attachment("", timeout, "audio")
        if result is None:
            outcome = "timeout"
        else:
            outcome = "audio" if result.get("isAudio") else "wrong"
        return {"outcome": outcome, "elapsed": round(clock.now, 1)}
    except scope["MuseGenerationError"] as exc:
        return {"outcome": type(exc).__name__, "elapsed": round(clock.now, 1),
                "error": str(exc)}


def cases(source):
    result = {}
    result["audio_ready"] = scenario(source, cards_at=10)
    result["stop_lifecycle"] = scenario(source, cards_at=10, stop_until=20)
    result["audio_and_video"] = scenario(source, cards=[video_card(), audio_card()])
    result["video_only"] = scenario(source, cards=[video_card()], timeout=20,
                                    text="视频已生成，文件 clip.mp4")
    result["image_only"] = scenario(source, cards=[image_card()], timeout=20,
                                    text="图片已生成，文件 pic.png")
    result["no_attachment"] = scenario(source, cards_at=None, timeout=20)
    result["explicit_refusal"] = scenario(
        source, cards_at=None, text="音频生成失败：当前账号不支持语音合成。")
    return result


def main():
    source = ROOT / "engine.py"
    result = cases(source)
    for name in ("audio_ready", "audio_and_video"):
        assert result[name]["outcome"] == "audio", (name, result[name])
    assert result["audio_ready"]["elapsed"] >= 10, result["audio_ready"]
    assert result["stop_lifecycle"]["outcome"] == "audio", result["stop_lifecycle"]
    assert result["stop_lifecycle"]["elapsed"] >= 20, result["stop_lifecycle"]
    for name in ("video_only", "image_only", "no_attachment"):
        assert result[name]["outcome"] == "timeout", (name, result[name])
    assert result["explicit_refusal"]["outcome"] == "MuseModelRefusalError", result["explicit_refusal"]
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    print("PASS: audio card selection, stop lifecycle, modality isolation and refusal")


if __name__ == "__main__":
    main()
