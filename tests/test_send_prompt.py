"""Send waits out reference-image uploads instead of giving up in 3 seconds."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import MuseEngine, send_failure_message, send_wait_budget


def main():
    assert send_wait_budget(0) == 12.0
    assert send_wait_budget(None) == 12.0
    assert send_wait_budget(1) == 14.0
    assert send_wait_budget(4) == 32.0
    assert send_wait_budget(10) == 45.0
    assert send_wait_budget(99) == 45.0

    disabled = send_failure_message("disabled")
    missing = send_failure_message("missing")
    assert disabled.startswith("提示词发送未确认")
    assert "发送按钮仍不可用" in disabled
    assert "未找到发送按钮" in missing
    assert "提示词没有写进输入框" in send_failure_message("no-text")

    js = MuseEngine._COMPOSER_JS
    assert "innerText" not in js
    assert "offsetParent" not in js
    assert "al === 'send'" in js
    assert "hatch-composer-send" in js
    assert "remove attachment" in js
    clear = MuseEngine._clear_attachments.__doc__ or ""
    assert "移除附件" in clear
    print("PASS send prompt")


if __name__ == "__main__":
    main()
