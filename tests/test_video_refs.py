"""Video requests accept up to 10 reference images, in order."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main():
    with tempfile.TemporaryDirectory(prefix="muse-video-refs-") as tmp:
        os.environ["MUSE2API_HOME"] = tmp
        os.environ["MUSE2API_KEY"] = "test-secret"
        os.environ["UPLOAD_TOKEN"] = "test-token"
        os.environ["REDIS_URL"] = "memory://"
        os.environ["MUSE2API_REDIS_URL"] = "memory://"
        spec = importlib.util.spec_from_file_location("video_refs_app", ROOT / "app.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["video_refs_app"] = module
        spec.loader.exec_module(module)

        req = module.VideoRequest(
            prompt="红色变成蓝色",
            duration=6,
            size="16:9",
            reference_images=["https://example.com/a.png", "https://example.com/b.png"],
            reference_image="https://example.com/a.png",
        )
        refs = module.video_reference_images(req)
        assert refs == ["https://example.com/a.png", "https://example.com/b.png"], refs
        prompt = module.build_video_prompt(req, ref_count=len(refs))
        assert "2 张参考图" in prompt
        assert "时长严格为 6 秒" in prompt

        single = module.VideoRequest(prompt="走动", image_url={"url": "https://example.com/one.png"})
        assert module.video_reference_images(single) == ["https://example.com/one.png"]
        one_prompt = module.build_video_prompt(single)
        assert "第一帧" in one_prompt
        assert "张参考图" not in one_prompt

        plain = module.VideoRequest(prompt="纯文字")
        assert module.video_reference_images(plain) == []
        assert "全新文生视频" in module.build_video_prompt(plain)

        data_url = module.VideoRequest(prompt="图", images=[{"b64_json": "abcd"}])
        assert module.video_reference_images(data_url) == ["data:image/png;base64,abcd"]

        too_many = module.VideoRequest(prompt="多", reference_images=[f"https://example.com/{i}.png" for i in range(11)])
        try:
            module.video_reference_images(too_many)
        except module.MuseGenerationError as exc:
            assert "10" in str(exc)
        else:
            raise AssertionError("expected the 11th reference image to be rejected")

    print("PASS video reference images")


if __name__ == "__main__":
    main()
