"""Offline HTTP and FFmpeg regression: previews must never download originals."""
import asyncio
import importlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_media_delete import Client


async def main():
    assert shutil.which("ffmpeg"), "Install ffmpeg to run thumbnail integration tests"
    with tempfile.TemporaryDirectory(prefix="muse-thumbnails-") as tmp:
        os.environ["MUSE2API_HOME"] = tmp
        os.environ["MUSE2API_KEY"] = "fixture-only"
        module = importlib.import_module("app")
        import thumbnails
        media = Path(module.CFG.media_dir)
        cache = Path(tmp) / "data" / "thumbnails"
        name = "preview #1.mp4"
        source = media / name
        subprocess.run([
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
            "testsrc2=size=960x540:rate=24", "-t", "1", "-c:v", "libx264",
            "-threads", "1", "-pix_fmt", "yuv420p", str(source),
        ], check=True)
        original = source.read_bytes()
        path = f"/v1/media/{name}/thumbnail"
        async with Client(module.app, {"Authorization": "Bearer fixture-only"}) as client:
            # Listing only stats files, and never starts a decoder.
            with patch.object(thumbnails.subprocess, "run", side_effect=AssertionError("eager decode")):
                listed = (await client.get("/admin/media")).json()
            item = listed["media"][0]
            assert listed["count"] == 1 and item["kind"] == "video"
            assert "preview%20%231.mp4/thumbnail?v=" in item["thumbnail_url"]
            assert not cache.exists()

            # Concurrent requests share one generation; repeats reuse the JPEG.
            with patch.object(thumbnails.subprocess, "run", wraps=subprocess.run) as run:
                responses = await asyncio.gather(*(client.get(path) for _ in range(5)))
                assert all(r.status_code == 200 and r.body.startswith(b"\xff\xd8") for r in responses)
                assert len(responses[0].body) < len(original) / 2
                assert run.call_count == 1
                assert (await client.get(path)).body == responses[0].body
                assert run.call_count == 1
                # Replacing an existing video must invalidate both URL and cache.
                source.write_bytes(original)
                changed = (await client.get("/admin/media")).json()["media"][0]
                assert changed["thumbnail_url"] != item["thumbnail_url"]
                assert (await client.get(path)).status_code == 200
                assert run.call_count == 2
            assert len(list(cache.glob("*.jpg"))) == 1
            assert source.read_bytes() == original
            assert (await client.get("/admin/media")).json()["count"] == 1

            # Unsupported/missing/broken files never fall back to video bytes.
            (media / "broken.mp4").write_bytes(b"not a video")
            (media / "image.jpg").write_bytes(b"not a video")
            (media / "linked.mp4").symlink_to(source)
            for endpoint, expected in [
                ("broken.mp4", 503), ("missing.mp4", 404),
                ("image.jpg", 400), ("linked.mp4", 404), ("..\\escape.mp4", 400),
            ]:
                result = await client.get(f"/v1/media/{endpoint}/thumbnail")
                assert result.status_code == expected, (endpoint, result.status_code)
            assert len(list(cache.glob("*.jpg"))) == 1  # no partial files
            shutil.copyfile(source, media / "no-ffmpeg.mp4")
            with patch.object(thumbnails.subprocess, "run", side_effect=FileNotFoundError("ffmpeg")):
                assert (await client.get("/v1/media/no-ffmpeg.mp4/thumbnail")).status_code == 503

            # The original endpoint still supports ranges, only requested on play.
            result = await client.get(f"/v1/media/{name}", headers={"Range": "bytes=0-31"})
            assert result.status_code == 206 and result.body == original[:32]
            result = await client.post("/admin/media/delete", json_data={"names": [name]})
            assert result.json()["removed"] == 1 and not list(cache.glob("*.jpg"))
            assert (await client.get(path)).status_code == 404
            source.write_bytes(original)
            assert (await client.get(path)).status_code == 200
            assert (await client.delete(f"/admin/media/{name}")).status_code == 200
            assert not list(cache.glob("*.jpg"))
    print("PASS: lazy list, JPEG/cache/concurrency/invalidation, failures, ranges and deletion")


if __name__ == "__main__":
    asyncio.run(main())
