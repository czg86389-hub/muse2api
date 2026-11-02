"""Static regression: hatch_gw must never block cookie import."""
import ast
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_IMPORT = ("hatch_sess",)


def python_constant(path: Path, name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            target = node.targets[0] if isinstance(node, ast.Assign) else node.target
            if isinstance(target, ast.Name) and target.id == name:
                return tuple(ast.literal_eval(node.value))
    raise AssertionError(f"{name} not found in {path}")


def js_constant(path: Path, name: str):
    text = path.read_text(encoding="utf-8")
    match = re.search(rf"const\s+{name}\s*=\s*(\[[^;]+\])", text)
    assert match, (path, name)
    return tuple(json.loads(match.group(1).replace("'", '"')))


engine = python_constant(ROOT / "engine.py", "ESSENTIAL_COOKIES")
store = python_constant(ROOT / "store.py", "ESSENTIAL_COOKIES")
helper = python_constant(ROOT / "tools/get_muse_cookie.py", "ESSENTIAL")
extension = js_constant(ROOT / "extension/popup.js", "ESSENTIAL")
userscript = js_constant(ROOT / "tools/muse2api_cookie_importer.user.js", "ESSENTIAL")

assert engine == store == helper == extension == userscript == EXPECTED_IMPORT, (
    engine, store, helper, extension, userscript)
assert "hatch_gw" not in engine + helper + extension + userscript
manifest = json.loads((ROOT / "extension/manifest.json").read_text(encoding="utf-8"))
assert tuple(map(int, manifest["version"].split("."))) >= (1, 0, 1)
admin = (ROOT / "admin.html").read_text(encoding="utf-8")
assert "muse.ai 的 4 条核心 cookie" not in admin
assert "muse.ai's 4 core cookies" not in admin
assert "缺少时不阻止导入" in admin
assert "Required cookies:" in admin
assert "does not block import" in admin
print("PASS: server and every cookie importer require only hatch_sess; hatch_vml is optional")
