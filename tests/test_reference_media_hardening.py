"""Bounded, offline checks for PR #40 validation, cleanup and result filtering."""
import base64
import hashlib
import math
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import (MuseEngine, MuseGenerationError, _mp4_duration, _webm_duration)

def element(tag, payload):
    assert len(payload) < 127
    return tag + bytes([0x80 | len(payload)]) + payload

def webm(seconds, scale=1000000, unknown_segment=False):
    info = element(b"\x2a\xd7\xb1", scale.to_bytes(4, "big"))
    info += element(b"\x44\x89", struct.pack(">d", seconds * 1e9 / scale))
    info = element(b"\x15\x49\xa9\x66", info)
    segment = (b"\x18\x53\x80\x67\xff" + info if unknown_segment
               else element(b"\x18\x53\x80\x67", info))
    return element(b"\x1a\x45\xdf\xa3", b"") + segment

def data_url(mime, value):
    return "data:" + mime + ";base64," + base64.b64encode(value).decode()

def reject(engine, mime, data):
    try:
        engine._normalize_image(data_url(mime, data))
    except MuseGenerationError:
        return
    raise AssertionError("invalid reference accepted: " + mime)

with tempfile.TemporaryDirectory() as home:
    engine = MuseEngine(SimpleNamespace(profile_dir=home, media_dir=home))
    for unknown in (False, True):
        assert _webm_duration(webm(120, unknown_segment=unknown)) == 120
        assert engine._normalize_image(data_url("video/webm", webm(120, unknown_segment=unknown)))[1] == "video/webm"
    assert _webm_duration(webm(12.5, scale=100000)) == 12.5
    for value in (120.001, math.inf, math.nan, 0, -1):
        reject(engine, "video/webm", webm(value))
    for data in (b"garbage", b"\x1a\x45\xdf\xa3", webm(2)[:-2]):
        reject(engine, "video/webm", data)
    reject(engine, "video/mp4", b"not a video")
    reject(engine, "application/octet-stream", b"not a known media file")
    print("PASS: WebM finite/unknown Segment, time scale, duration limits and malformed metadata")

    originals = [("reference-" + str(i)).encode() for i in range(10)]
    queue = [{"src": "upload-" + str(i), "vSrc": "upload-" + str(i)} for i in range(10)]
    queue.append({"src": "result", "vSrc": "result"})
    engine.attachments = lambda: []
    engine._agent_count = lambda: 0
    engine._send = lambda prompt: "clicked"
    engine._wait_attachment = lambda *a, **k: queue.pop(0)
    engine.extract_bytes = lambda src, expect: (
        originals[int(src.split("-")[1])] if src.startswith("upload-") else b"generated-result",
        "video/mp4", src)
    with patch("engine.time.sleep", lambda value: None):
        result = engine._generate_attached("fixture", "video", float("inf"), None, None,
                                           {hashlib.sha256(x).hexdigest() for x in originals})
    assert Path(result["path"]).read_bytes() == b"generated-result"
    assert not queue
    print("PASS: ten original uploads are skipped before delivering the actual result")

    engine._clear_attachments = lambda: None
    engine._set_input_files = lambda paths: None
    def disconnect(_):
        raise RuntimeError("synthetic CDP disconnect")
    engine.page = SimpleNamespace(js=disconnect)
    before = set(Path(home).iterdir())
    with patch("engine.time.sleep", lambda value: None):
        try:
            engine._attach_images([data_url("image/png", b"fixture")])
        except RuntimeError as exc:
            assert "synthetic" in str(exc)
        else:
            raise AssertionError("expected a CDP exception")
    assert set(Path(home).iterdir()) == before
    engine.page = SimpleNamespace(js=lambda script: 1)
    with patch("engine.time.sleep", lambda value: None):
        upload = engine._attach_images([data_url("image/png", b"fixture")])
    assert Path(upload["dir"]).parent == Path(home)
    import shutil
    shutil.rmtree(upload["dir"])
    assert set(Path(home).iterdir()) == before
    print("PASS: confirmation exceptions clean files, and uploads use the Chromium-accessible profile")
