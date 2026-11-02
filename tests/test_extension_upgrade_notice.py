from pathlib import Path
import json
root=Path(__file__).resolve().parents[1]
v=json.loads((root/'version.json').read_text())
m=json.loads((root/'extension/manifest.json').read_text())
assert v['version']=='1.6.6' and m['version']=='1.0.2'
assert '1.0.2' in v['highlights'][0] and '1.0.2' in v['highlights_en'][0]
assert '不会自动' in v['highlights'][0] and 'each browser profile' in v['highlights_en'][0]
html=(root/'admin.html').read_text()
assert 'id="extensionUpgradeNotice"' in html and '"extension-upgrade-notice":' in html
assert 'releases/download/v1.6.6/muse2api-chrome-extension-v1.6.6.zip' in html
assert 'id="extensionVersion"' in (root/'extension/popup.html').read_text()
assert '@version      1.1.2' in (root/'tools/muse2api_cookie_importer.user.js').read_text()
print('PASS: bilingual upgrade notice, version badge, release instructions and download link')
