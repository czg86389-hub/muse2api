"""Offline regressions for video / audio / document references. No account, network or Chromium."""
import asyncio
import base64
import hashlib
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def import_app(home):
    os.environ.update(MUSE2API_HOME=home, MUSE2API_PROFILE_ROOT=home,
                      MUSE2API_KEY="test-only")
    spec = importlib.util.spec_from_file_location("muse_media_candidate", ROOT / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def box(kind, body):
    return (8 + len(body)).to_bytes(4, "big") + kind + body


def mp4(seconds, brand=b"isom"):
    mvhd = box(b"mvhd", b"\0" * 12 + (1000).to_bytes(4, "big")
               + int(seconds * 1000).to_bytes(4, "big") + b"\0" * 80)
    return (box(b"ftyp", brand + b"\0\0\2\0" + brand + b"mp41")
            + box(b"moov", mvhd) + box(b"mdat", b"x" * 16))


def data_url(mime, data):
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def expect_error(fn, exc_type, text):
    try:
        fn()
    except exc_type as exc:
        message = str(getattr(exc, "detail", exc))
        assert text in message, (text, message)
    else:
        raise AssertionError(f"expected {exc_type.__name__} containing {text!r}")


WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\0" * 24
MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x0f" + b"\xff\xfb\x90\x00" * 8
PDF = b"%PDF-1.4\n%fixture"
WEBM = b"\x1a\x45\xdf\xa3" + b"\0" * 16

with tempfile.TemporaryDirectory() as home:
    app = import_app(home)
    engine_module = sys.modules[app.MuseEngine.__module__]
    sniff = engine_module._sniff_mime
    assert sniff(mp4(5)) == "video/mp4"
    assert sniff(mp4(5, b"qt  ")) == "video/quicktime"
    assert sniff(mp4(5, b"M4A ")) == "audio/mp4"
    assert sniff(WEBM) == "video/webm"
    assert sniff(WAV) == "audio/wav"
    assert sniff(MP3) == "audio/mpeg"
    assert sniff(PDF) == "application/pdf"
    assert sniff(b"plain text") is None
    assert round(engine_module._mp4_duration(mp4(12.5)), 1) == 12.5

    engine = app.MuseEngine(SimpleNamespace(profile_dir=home))
    norm = engine._normalize_image
    assert norm(data_url("video/mp4", mp4(10)))[1] == "video/mp4"
    assert norm(data_url("application/octet-stream", WAV))[1] == "audio/wav"
    assert norm(data_url("audio/mp3", MP3))[1] == "audio/mpeg"
    assert norm(data_url("", PDF))[1] == "application/pdf"
    assert norm(base64.b64encode(mp4(3)).decode())[1] == "video/mp4"
    expect_error(lambda: norm(data_url("text/plain", b"x")), app.MuseGenerationError, "不受支持")
    expect_error(lambda: norm(data_url("video/mp4", mp4(130))), app.MuseGenerationError, "120 秒")
    print("PASS: sniffing, MIME aliases, unsupported types and video duration limit")

    req = app.VideoRequest(prompt="x", images=["img"], videos=[{"video_url": "vid"}],
                           audios=[{"url": "aud"}])
    assert app._video_references(req) == ["img", "vid", "aud"]
    expect_error(lambda: app._video_references(app.VideoRequest(prompt="x", videos=[{}])),
                 app.HTTPException, "videos[0]")
    expect_error(lambda: app._video_references(app.VideoRequest(
        prompt="x", images=["a"] * 6, videos=["b"] * 5)), app.HTTPException, "最多 10 个")

    prepared = app._prepare_reference_images([
        data_url("image/png", b"png"), data_url("video/mp4", mp4(4)), data_url("audio/wav", WAV)])
    kinds = app._reference_kinds(prepared)
    assert kinds == ["image", "video", "audio"]
    prompt = app.build_video_prompt(req, kinds)
    assert "1 张图片、1 段视频、1 段音频" in prompt and "第一张" not in prompt
    assert "附件对照：[图1] 是第 1 个附件（图片），[视频1] 是第 2 个附件（视频），[音频1] 是第 3 个附件（音频）" in prompt
    tokens = app.build_video_prompt(app.VideoRequest(prompt="[图2] 走进 [图1]", images=["a", "b"]), ["image", "image"])
    assert "[图2] 是第 2 个附件（图片）" in tokens and "未说明时以第一张" in tokens
    plain = app.build_video_prompt(app.VideoRequest(prompt="x", images=["a", "b"]), ["image", "image"])
    assert "附件对照" not in plain and "以第一张作为主要起始画面" in plain
    two_videos = app.build_video_prompt(app.VideoRequest(prompt="x", videos=["a", "b"]), ["video", "video"])
    assert "[视频2] 是第 2 个附件（视频）" in two_videos
    assert "图生视频" not in app.build_video_prompt(app.VideoRequest(prompt="x", videos=["v"], size="16:9"), ["video"])
    assert "图生视频" in app.build_video_prompt(app.VideoRequest(prompt="x", image="i", size="16:9"))
    video_only = app.build_video_prompt(app.VideoRequest(prompt="x", videos=["v"]), ["video"])
    assert "1 段视频" in video_only and "第一张图片" not in video_only
    image_prompt = app.build_image_prompt(app.ImageRequest(prompt="x", videos=["v"]), ["video"])
    assert "1 段视频" in image_prompt and "纯净画面" not in image_prompt
    assert "[视频1] 是第 1 个附件（视频）" in image_prompt
    image_tokens = app.build_image_prompt(app.ImageRequest(prompt="[图1] 穿上 [图2] 的衣服", images=["a", "b"]), ["image", "image"])
    assert "附件对照：[图1] 是第 1 个附件（图片），[图2] 是第 2 个附件（图片）" in image_tokens
    legacy = app.build_video_prompt(app.VideoRequest(prompt="x", image="i"))
    assert "第一帧参考图" in legacy
    print("PASS: ordered media fields, count limit and media-aware prompts")

    class FakeForm(dict):
        def __init__(self, files, **values):
            super().__init__(values)
            self.files = files

        def getlist(self, key):
            return self.files.get(key, [])

    class FakeUpload:
        def __init__(self, content, content_type):
            self.content, self.content_type = content, content_type

        async def read(self, limit=-1):
            return self.content if limit < 0 else self.content[:limit]

    class FakeMultipart:
        headers = {"content-type": "multipart/form-data; boundary=test"}

        def __init__(self, form):
            self.value = form

        async def form(self, **kwargs):
            return self.value

    captured = {}
    original_queue = app._queue_image
    try:
        app._queue_image = lambda req, prompt, refs, *a, **k: captured.update(
            refs=refs, prompt=prompt) or {"ok": True}
        form = FakeForm({"image": [FakeUpload(b"\x89PNG\r\n\x1a\nfixture", "image/png")],
                         "video": [FakeUpload(mp4(6), "application/octet-stream")],
                         "audio": [FakeUpload(MP3, "audio/mpeg")]},
                        prompt="x", **{"async": True})
        assert asyncio.run(app.images_edits(FakeMultipart(form), None)) == {"ok": True}
        assert app._reference_kinds(captured["refs"]) == ["image", "video", "audio"]
        assert "1 段视频" in captured["prompt"]

        bad = FakeForm({"audio": [FakeUpload(b"x", "text/html")]}, prompt="x", **{"async": True})
        try:
            asyncio.run(app.images_edits(FakeMultipart(bad), None))
        except app.HTTPException as exc:
            assert exc.status_code == 400 and "audio[0]" in exc.detail
        else:
            raise AssertionError("unsupported multipart type was accepted")
    finally:
        app._queue_image = original_queue
    print("PASS: multipart image/video/audio fields")


    # 页面把刚上传的素材渲染成"结果"时，按哈希忽略并继续等待真正的成品
    media = Path(home, "media")
    guard = app.MuseEngine(SimpleNamespace(profile_dir=home, media_dir=str(media)))
    upload_bytes, result_bytes = mp4(4), mp4(5) + b"generated"
    hits = iter([{"src": "poster", "vSrc": "blob:upload", "hasVideo": True, "w": 9, "h": 9},
                 {"src": "poster2", "vSrc": "blob:result", "hasVideo": True, "w": 9, "h": 9}])
    seen_baselines = []
    guard.attachments = lambda: []
    guard._agent_count = lambda: 0
    guard._send = lambda prompt: "clicked"

    def fake_wait(*args, baseline_sources=None, **kwargs):
        seen_baselines.append(set(baseline_sources))
        return next(hits)

    guard._wait_attachment = fake_wait
    guard.extract_bytes = lambda src, expect: (
        upload_bytes if src == "blob:upload" else result_bytes, "video/mp4", src)
    res = guard._generate_attached("x", "video", float("inf"), None, None,
                                   {hashlib.sha256(upload_bytes).hexdigest()})
    assert Path(res["path"]).read_bytes() == result_bytes
    assert "blob:upload" in seen_baselines[1]
    print("PASS: uploaded reference is never delivered as the generated result")
