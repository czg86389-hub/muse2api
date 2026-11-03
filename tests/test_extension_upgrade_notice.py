from pathlib import Path
import json
root=Path(__file__).resolve().parents[1]
v=json.loads((root/'version.json').read_text())
m=json.loads((root/'extension/manifest.json').read_text())
assert m['version']=='1.0.2'
assert m['version'] in '\n'.join(v['highlights']) and m['version'] in '\n'.join(v['highlights_en'])
assert '手动' in '\n'.join(v['highlights']) and 'every browser profile' in '\n'.join(v['highlights_en'])
html=(root/'admin.html').read_text()
assert 'id="extensionUpgradeNotice"' in html and '"extension-upgrade-notice":' in html
assert f"releases/download/v{v['version']}/muse2api-chrome-extension-v{v['version']}.zip" in html
assert 'id="extensionVersion"' in (root/'extension/popup.html').read_text()
assert '@version      1.1.2' in (root/'tools/muse2api_cookie_importer.user.js').read_text()
print('PASS: bilingual upgrade notice, version badge, release instructions and download link')
