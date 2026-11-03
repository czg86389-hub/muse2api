"""Offline/API regressions for multi-reference validation and memory limits."""
import asyncio
import base64
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def import_app(home):
    os.environ.update(MUSE2API_HOME=home, MUSE2API_PROFILE_ROOT=home,
                      MUSE2API_KEY="test-only")
    spec = importlib.util.spec_from_file_location("muse_reference_candidate", ROOT / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


with tempfile.TemporaryDirectory() as home:
    app = import_app(home)
    assert app._image_references(app.ImageRequest(
        prompt="x", images=[{"url": "first"}, {"image_url": {"url": "second"}}])) == ["first", "second"]

    for request in [
        app.ImageRequest(prompt="x", images=[None]),
        app.ImageRequest(prompt="x", images=["a"] * (app.MAX_REFERENCE_IMAGES + 1)),
        app.ImageRequest(prompt="x", reference_image="old", images=["new"]),
    ]:
        try:
            app._image_references(request)
        except app.HTTPException as exc:
            assert exc.status_code == 400
        else:
            raise AssertionError("invalid image references were silently accepted")

    for request in [
        app.VideoRequest(prompt="x", images=[{}]),
        app.VideoRequest(prompt="x", image="old", images=["new"]),
    ]:
        try:
            app._video_references(request)
        except app.HTTPException as exc:
            assert exc.status_code == 400
        else:
            raise AssertionError("invalid video image references were silently accepted")

    engine = app.MuseEngine(SimpleNamespace(profile_dir=home))
    valid = "data:image/png;base64," + base64.b64encode(b"ok").decode()
    assert engine._normalize_image(valid)[1] == "image/png"
    for invalid in [
        "data:text/plain;base64," + base64.b64encode(b"not image").decode(),
        "data:image/png;base64,%%bad%%",
    ]:
        try:
            engine._normalize_image(invalid)
        except app.MuseGenerationError:
            pass
        else:
            raise AssertionError("invalid image payload was accepted")

    oversized = "data:image/png;base64," + base64.b64encode(
        b"x" * (sys.modules[app.MuseEngine.__module__].MAX_REFERENCE_IMAGE_BYTES + 1)).decode()
    try:
        engine._normalize_image(oversized)
    except app.MuseGenerationError as exc:
        assert f"{app.MAX_REFERENCE_IMAGE_BYTES // (1024 * 1024)} MiB" in str(exc)
    else:
        raise AssertionError("oversized image was accepted")

    try:
        engine._attach_images([valid] * (app.MAX_REFERENCE_IMAGES + 1))
    except app.MuseGenerationError as exc:
        assert f"最多 {app.MAX_REFERENCE_IMAGES} 个" in str(exc)
    else:
        raise AssertionError("more references than the limit were accepted")

print("PASS: reference count, item, conflict, MIME and byte limits")



class FakeJSONRequest:
    headers = {"content-type": "application/json"}

    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


class FakeForm(dict):
    def __init__(self, images, **values):
        super().__init__(values)
        self._images = images

    def getlist(self, key):
        return self._images if key == "image" else []


class FakeMultipartRequest:
    headers = {"content-type": "multipart/form-data; boundary=test"}

    def __init__(self, form):
        self.value = form
        self.form_options = None

    async def form(self, **kwargs):
        self.form_options = kwargs
        return self.value


class FakeUpload:
    def __init__(self, content, content_type="image/png"):
        self.content = content
        self.content_type = content_type
        self.read_limit = None

    async def read(self, limit=-1):
        self.read_limit = limit
        return self.content if limit < 0 else self.content[:limit]


async def expect_endpoint_400(request, text):
    try:
        await app.images_edits(request, None)
    except app.HTTPException as exc:
        assert exc.status_code == 400
        assert text in str(exc.detail), (text, exc.detail)
    else:
        raise AssertionError("invalid /v1/images/edits request was accepted")


# Exercise the real endpoint path, not only its helper.
for body, text in [
    ({"prompt": "x", "async": True, "images": [None]}, "images[0]"),
    ({"prompt": "x", "async": True, "images": ["new"], "image": "old"}, "不能"),
    ({"prompt": "x", "async": True, "images": "not-an-array"}, "必须是数组"),
]:
    asyncio.run(expect_endpoint_400(FakeJSONRequest(body), text))

captured = {}
original_queue = app._queue_image
try:
    app._queue_image = lambda req, prompt, refs, *args, **kwargs: captured.update(refs=refs) or {"ok": True}
    response = asyncio.run(app.images_edits(FakeJSONRequest({
        "prompt": "x", "async": True, "images": ["data:image/png;base64,Zmlyc3Q=", {"url": "data:image/png;base64,c2Vjb25k"}]
    }), None))
    assert response == {"ok": True}
    assert captured["refs"] == ["data:image/png;base64,Zmlyc3Q=",
                            "data:image/png;base64,c2Vjb25k"]
finally:
    app._queue_image = original_queue

oversized = FakeUpload(b"x" * (app.MAX_REFERENCE_IMAGE_BYTES + 1))
oversized_request = FakeMultipartRequest(FakeForm(
    [oversized], prompt="x", **{"async": True}))
asyncio.run(expect_endpoint_400(oversized_request, f"{app.MAX_REFERENCE_IMAGE_BYTES // (1024 * 1024)} MiB"))
assert oversized.read_limit == app.MAX_REFERENCE_IMAGE_BYTES + 1
assert oversized_request.form_options == {
    "max_files": app.MAX_REFERENCE_IMAGES, "max_fields": 20}

chunk = b"x" * (app.MAX_REFERENCE_TOTAL_BYTES // 3 + 1)
total_request = FakeMultipartRequest(FakeForm(
    [FakeUpload(chunk), FakeUpload(chunk), FakeUpload(chunk)],
    prompt="x", **{"async": True}))
asyncio.run(expect_endpoint_400(total_request, f"{app.MAX_REFERENCE_TOTAL_BYTES // (1024 * 1024)} MiB"))

count_request = FakeMultipartRequest(FakeForm(
    ["x"] * (app.MAX_REFERENCE_IMAGES + 1), prompt="x"))
asyncio.run(expect_endpoint_400(count_request, f"最多 {app.MAX_REFERENCE_IMAGES} 个"))

print("PASS: endpoint JSON conflicts plus multipart count, per-file and total byte limits")
