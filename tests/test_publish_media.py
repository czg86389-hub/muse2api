"""Upload generated media to the image host and delete the local file."""
import base64
import importlib.util
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SOURCE = ROOT / "app.py"


def main():
    with tempfile.TemporaryDirectory(prefix="muse-publish-test-") as tmp:
        os.environ["MUSE2API_HOME"] = tmp
        os.environ["MUSE2API_KEY"] = "test-secret"
        os.environ["UPLOAD_TOKEN"] = "test-token"
        os.environ["REDIS_URL"] = "memory://"
        os.environ["MUSE2API_REDIS_URL"] = "memory://"
        os.environ["IMAGE_UPLOAD_BASE_URL"] = "https://upload.openclaw-token.shop"
        spec = importlib.util.spec_from_file_location("publish_media_app", SOURCE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        media = Path(module.CFG.media_dir)
        media.mkdir(parents=True, exist_ok=True)
        src = media / "local.png"
        payload = b"png-bytes"
        src.write_bytes(payload)
        seen = {}
        real_upload = module.upload_to_image_host

        def fake_upload(path, filename, mime=None):
            seen["path"] = path
            seen["filename"] = filename
            seen["mime"] = mime
            assert open(path, "rb").read() == payload
            return {"ok": True,
                    "url": "https://source.openclaw-token.shop/uploads/remote.png",
                    "filename": "remote.png", "size": len(payload)}

        module.upload_to_image_host = fake_upload
        out = module.publish_generated_media(
            {"path": str(src), "filename": "local.png", "size": len(payload),
             "kind": "image", "mime": "image/png"},
            include_b64=True)
        assert not src.exists(), "local file must be removed after upload"
        assert out["url"].endswith("/remote.png")
        assert out["path"] is None
        assert out["filename"] == "remote.png"
        assert base64.b64decode(out["b64_json"]) == payload
        assert seen["filename"] == "local.png" and seen["mime"] == "image/png"

        failed = media / "fail.mp4"
        failed.write_bytes(b"mp4")

        def bad_upload(path, filename, mime=None):
            raise module.MuseGenerationError("图床上传失败 HTTP 502: r2")

        module.upload_to_image_host = bad_upload
        try:
            module.publish_generated_media(
                {"path": str(failed), "filename": "fail.mp4", "kind": "video"})
            raise AssertionError("upload failure must surface")
        except module.MuseGenerationError as exc:
            assert "图床上传失败" in str(exc)
        assert not failed.exists(), "failed upload must not leave a local file"

        module.upload_to_image_host = real_upload
        module.CFG.upload_token = ""
        empty = media / "empty.png"
        empty.write_bytes(b"x")
        try:
            module.upload_to_image_host(str(empty), "empty.png", "image/png")
            raise AssertionError("missing token must fail before upload")
        except module.MuseGenerationError as exc:
            assert "UPLOAD_TOKEN" in str(exc)
        assert empty.exists()
        print("PASS publish media uploads then deletes local file")


if __name__ == "__main__":
    main()
