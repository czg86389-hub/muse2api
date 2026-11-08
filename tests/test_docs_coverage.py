"""Static checks that the Integration Docs tab covers every endpoint and its I/O."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
html = (ROOT / "admin.html").read_text(encoding="utf-8")
start = html.index('id="tab-docs"')
docs = html[start:html.index("</section>", start)]

PUBLIC_ENDPOINTS = [
    "/healthz", "/readyz", "/v1/models",
    "/v1/chat/completions", "/v1/responses",
    "/v1/images/generations", "/v1/images/edits",
    "/v1/images/tasks", "/v1/images/tasks/{task_id}",
    "/v1/videos", "/v1/videos/generations", "/v1/videos/{task_id}",
    "/v1/audio/speech", "/v1/media/{name}",
]
ADMIN_ENDPOINTS = [
    "/admin/status", "/admin/dashboard", "/admin/settings",
    "/admin/accounts", "/admin/accounts/{id}/test",
    "/admin/accounts/{id}/quota", "/admin/accounts/{id}/cookies",
    "/admin/accounts/{id}/proxy/test", "/admin/accounts/{id}/relogin",
    "/admin/accounts/{id}/keepalive", "/admin/accounts/keepalive",
    "/admin/keepalive/status", "/admin/relogin", "/admin/quota",
    "/admin/apikey", "/admin/apikey/rotate",
    "/admin/tasks", "/admin/tasks/clear", "/admin/media",
    "/admin/media/delete", "/admin/cookie-helper",
    "/admin/extension", "/admin/extension/files",
    "/admin/update/check", "/admin/update/upgrade",
    "/admin/repo/status", "/admin/repo/pull", "/admin/repo/push",
]
INPUT_FIELDS = [
    "messages", "stream", "stream_options", "include_usage",
    "tools", "instructions", "max_output_tokens",
    "prompt", "images", "videos", "audios", "files",
    "reference_image", "image_url", "aspect_ratio", "response_format",
    "b64_json", "async", "duration", "resolution",
    "Idempotency-Key", "input", "voice", "speed",
]
OUTPUT_MARKERS = [
    "chat.completion.chunk", "response.output_text.delta", "data: [DONE]",
    "image.task", "video.task", "revised_prompt",
    "X-Muse-Audio-Format", "Content-Disposition", "browser_running",
]
STATUS_CODES = ["400", "401", "404", "409", "422", "429", "500", "502"]
SECTIONS = [
    "1. 可用模型", "2. 通用约定", "3. 接口总览",
    "4. 对话与代码（Chat Completions）", "4.3 Responses API",
    "5. 生图（Images Generations）", "6. 图像编辑（Images Edits）",
    "7. 生图异步任务（Images Tasks）",
    "8. 生视频（Videos）", "9. 语音合成（Audio Speech）",
    "10. 其他接口", "11. 错误与状态码", "12. Python（openai SDK 兼容）",
    "13. 管理接口", "14. 鉴权", "15. 接入各类客户端 / 智能体",
]
EN_HEADINGS = [
    "2. Conventions", "3. Endpoint overview", "11. Errors and status codes",
]


def missing(items):
    return [item for item in items if item not in docs]


assert not missing(PUBLIC_ENDPOINTS), f"undocumented public endpoints: {missing(PUBLIC_ENDPOINTS)}"
assert not missing(ADMIN_ENDPOINTS), f"undocumented admin endpoints: {missing(ADMIN_ENDPOINTS)}"
assert not missing(INPUT_FIELDS), f"undocumented input fields: {missing(INPUT_FIELDS)}"
assert not missing(OUTPUT_MARKERS), f"undocumented output markers: {missing(OUTPUT_MARKERS)}"
assert not missing(SECTIONS), f"missing doc sections: {missing(SECTIONS)}"
assert not missing(STATUS_CODES), f"missing status codes: {missing(STATUS_CODES)}"
assert not [h for h in EN_HEADINGS if h not in html], \
    f"missing English headings: {[h for h in EN_HEADINGS if h not in html]}"
assert docs.count("<table>") >= 12, docs.count("<table>")
assert docs.count('<pre class="code">') >= 7, docs.count('<pre class="code">')

print("PASS: integration docs cover public/admin endpoints, input fields, "
      "output markers and status codes")
