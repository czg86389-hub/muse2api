"""Static wiring checks for the extension's copy-cookie button."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "extension"


def main():
    manifest = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
    assert "clipboardWrite" in manifest["permissions"], manifest["permissions"]

    html = (EXT / "popup.html").read_text(encoding="utf-8")
    assert 'id="copy"' in html
    assert "复制 Cookie" in html
    assert "name=value" in html

    js = (EXT / "popup.js").read_text(encoding="utf-8")
    assert "async function copyCookies()" in js
    assert "navigator.clipboard.writeText(header)" in js
    assert "document.execCommand('copy')" in js
    assert "join('; ')" in js
    assert "addEventListener('click', copyCookies)" in js
    assert "'注意：缺少核心项 {missing}。'" in js

    print("PASS: extension copy-cookie button, clipboard fallback and manifest permission")


if __name__ == "__main__":
    main()
