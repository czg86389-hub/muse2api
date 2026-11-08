# MUSE2API

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-blue?logo=python" alt="Python Version" />
  <img src="https://img.shields.io/badge/FastAPI-0.115%2B-009688?logo=fastapi" alt="FastAPI" />
  <img src="https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker" alt="Docker" />
  <img src="https://img.shields.io/badge/API-OpenAI%20Compatible-green" alt="OpenAI API Compatible" />
  <img src="https://img.shields.io/badge/License-MIT-orange" alt="License" />
</p>

MUSE2API wraps the multimodal web app of **[muse.ai](https://muse.ai/)** behind a standard, **OpenAI-compatible REST API**. It drives a headless Chromium over CDP, reuses warm tabs and WebSocket tunnels, and manages browser sessions per account. The result is an API that supports chat, image generation and editing, video generation, text-to-speech, multi-account rotation, scheduled session renewal, and cloud VM keep-alive.

---

## Quick start

### Option 1: Docker Compose (recommended)

```bash
git clone https://github.com/nguyenduclong-ict/muse2api.git
cd muse2api
cp .env.example .env      # optional, set MUSE2API_KEY
docker compose up -d
```

Open the console at `http://<server-ip>:18610/admin?key=<your-MUSE2API_KEY>`. If `MUSE2API_KEY` is empty, the service generates one on first start and prints it to the log.

### Option 2: Bare metal (Ubuntu / Debian)

```bash
sudo apt-get update
sudo apt-get install -y chromium fonts-wqy-zenhei python3 python3-venv
cd /opt/muse2api
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
sudo cp deploy/muse2api.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now muse2api
```

### Reverse proxy

If you put Nginx, BaoTa, or 1Panel in front of the service, raise `proxy_read_timeout` to `600s`, turn off `proxy_buffering` (keeps SSE streaming smooth), and set `client_max_body_size` to about `200M` for large reference media. A ready-to-use example is in `deploy/nginx.example.conf`.

---

## Import an account

The service needs at least one logged-in muse.ai session.

**Chrome / Edge extension** (`extension/` folder):

1. Open `chrome://extensions` and enable **Developer mode**.
2. Click **Load unpacked** and select the `extension` directory.
3. Log in to [muse.ai](https://muse.ai/) in that browser.
4. Open the extension popup, enter your server URL and `MUSE2API_KEY`.
5. Click **Read and import**, or **Copy cookies** and paste them into the manual import box in the console.

**Tampermonkey userscript** (any browser): install `tools/muse2api_cookie_importer.user.js`, open muse.ai, and click the floating ⚡ button.

The extension must be version **1.0.2+** and the userscript **1.1.2+**. A server update does not upgrade an unpacked extension; replace the files and click **Reload** for each browser profile.

---

## Features

### Core API

- **Chat Completions and Responses**: `/v1/chat/completions` and `/v1/responses` (the default protocol of Codex), with SSE streaming (`stream=true`), multi-turn context, and system prompts. Warm-tab affinity keeps the first token at roughly 2-3 seconds on consecutive conversations. Common model names (`gpt-4o`, `gpt-5`, `claude-sonnet-4`, `deepseek-chat`, and others) are mapped automatically.
- **Images**: `/v1/images/generations` and `/v1/images/edits` with multiple aspect ratios (`1:1`, `16:9`, `9:16`, `4:3`, `3:4`), single or ordered multi-image references, `url` or `b64_json` responses, and fast detection of text-only refusals.
- **Videos**: `/v1/videos` creates a task and `/v1/videos/{task_id}` reports progress. Request 5/6/8/10 second clips in `9:16` or `16:9`, with an optional first-frame image or an ordered `images` array. Results are stored and served from `/v1/media/{filename}`.
- **Text to speech**: `/v1/audio/speech` returns binary audio (MP3 by default) and works with `client.audio.speech.create`. `voice` accepts a free-text description, and formats other than Muse's native output are transcoded with `ffmpeg` when it is installed.
- **Async image tasks**: `POST /v1/images/tasks` plus polling on `GET /v1/images/tasks/{id}` avoids proxy or CDN timeouts on long generations. `async=true` switches the sync image endpoints to the same queue.
- **Model listing**: `/v1/models` returns the supported aliases.

### Recent additions in this build

- **Operations dashboard**: the console now opens on a dashboard with service health, account status, remaining quota, a 7-day task trend, recent tasks, and the media library. Live health, pool quota, one-click keep-alive for every account, and task progress are on the same page.
- **Global system prompt** (off by default): enable it in the console **Settings** page to prepend a `<system>...</system>` block to chat, Responses, image, video, and speech requests. Edit, reset, or set it with `MUSE2API_SYSTEM_PROMPT_ENABLED` and `MUSE2API_SYSTEM_PROMPT`; `data/settings.json` takes precedence after saving.
- **Media retention and startup sweep**: media is kept for 1 day and capped at 1 GB by default, scanned every 60 minutes. `data/downloads/context-*`, `muse2api-refs-*`, and expired `debug-*.json` files are removed at startup. Both limits are editable in **Settings** and can be run manually from `POST /admin/cleanup/run`.
- **Per-account proxy with verified status**: each account can use its own HTTP or SOCKS5 proxy. Test an unsaved route before saving, and the console shows a persisted connectivity result (reachability, egress IP, and session status) instead of guessing.
- **Higher default concurrency**: strict LRU round-robin across accounts for chat, image, and video tasks, with `MUSE2API_CONCURRENCY` now defaulting to 8. Accounts never collide; separate browser contexts run in parallel.
- **Extension cookie copy**: the popup can copy the current muse.ai cookies to the clipboard for manual import, useful on browsers where automatic push is blocked.

### Operational features

- **Real multi-account parallelism**: Chromium browser contexts isolate cookies and sessions, so images, videos, and streams run concurrently without interference. A scheduler keeps the same account exclusive and spreads work across healthy accounts.
- **Account pool with failover**: import any number of accounts. Warm-tab affinity and LRU balancing keep latency low, and an exhausted or unhealthy account is skipped immediately.
- **Session health and VM keep-alive**: `/api/session` checks the login and wakes the workspace through `/api/hatch/vm/wake` when needed, using the account proxy. Only cookies actually returned upstream are stored, and the expiry is never inflated by a successful probe.
- **Guarded reference media**: images, videos, audio, and documents up to 10 items per request, 40 MiB each and 100 MiB total. Local file paths and private-network URLs are rejected unless explicitly enabled.
- **Online update check**: the console shows a banner when the upstream repository publishes a new version, with a one-click pull and graceful restart that keeps `.env` and `data/`.

---

## API examples

All protected endpoints require:

```http
Authorization: Bearer <MUSE2API_KEY>
```

### Chat

```bash
curl -X POST "http://localhost:18610/v1/chat/completions" \
  -H "Authorization: Bearer m2a_your_secret_key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "muse-spark",
    "messages": [{"role": "user", "content": "Write a haiku about neon rain."}],
    "stream": false
  }'
```

### Image generation

```bash
curl -X POST "http://localhost:18610/v1/images/generations" \
  -H "Authorization: Bearer m2a_your_secret_key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "muse-image",
    "prompt": "A mechanical shiba inu in an anti-gravity suit, cinematic lighting, 8k",
    "size": "16:9",
    "response_format": "url"
  }'
```

Use the `images` array for multiple references (do not mix it with the legacy single-image fields `image` or `reference_image`). For `/v1/images/edits`, repeat the multipart `image` field.

### Video

```bash
curl -X POST "http://localhost:18610/v1/videos" \
  -H "Authorization: Bearer m2a_your_secret_key" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Golden maple leaves drifting in the wind", "duration": 5, "size": "16:9"}'

curl "http://localhost:18610/v1/videos/task_xyz789" \
  -H "Authorization: Bearer m2a_your_secret_key"
```

### Speech

```bash
curl -X POST "http://localhost:18610/v1/audio/speech" \
  -H "Authorization: Bearer m2a_your_secret_key" \
  -H "Content-Type: application/json" \
  -d '{"model": "muse-voice", "input": "Hello from MUSE2API.", "voice": "clear female voice"}' \
  --output speech.mp3
```

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:18610/v1", api_key="m2a_your_secret_key")
client.audio.speech.create(model="muse-voice", voice="clear female voice",
                           input="Hello from MUSE2API.").write_to_file("speech.mp3")
```

Muse's speech RPC accepts roughly 4 MiB and 20 seconds per call, so split long text into paragraphs and concatenate the audio.

---

## Configuration

Set variables in `.env` or in the service environment. Runtime settings saved from the console are stored in `data/settings.json` and take precedence.

| Variable | Default | Purpose |
|---|---|---|
| `MUSE2API_KEY` | auto-generated | Console and API key (`m2a_...`) |
| `MUSE2API_HOST` | `0.0.0.0` | Listen address |
| `MUSE2API_PORT` | `18610` | Listen port |
| `MUSE2API_PUBLIC_BASE` | empty | Public URL prefix; detected from the browser when empty |
| `MUSE2API_CHROMIUM` | auto-detect | Chromium/Chrome executable |
| `MUSE2API_CDP_PORT` | `19210` | Internal CDP port |
| `MUSE2API_CONCURRENCY` | `8` | Maximum concurrent generation tasks |
| `MUSE2API_IMAGE_TIMEOUT` | `240` | Image timeout (seconds) |
| `MUSE2API_VIDEO_TIMEOUT` | `600` | Video timeout (seconds) |
| `MUSE2API_AUDIO_TIMEOUT` | `180` | Speech timeout (seconds) |
| `MUSE2API_CHAT_TIMEOUT` | `300` | Chat timeout (seconds) |
| `MUSE2API_AUDIO_VOICE` | `自然清晰的女声` | Default voice description |
| `MUSE2API_SYSTEM_PROMPT_ENABLED` | `0` | Attach the global system prompt to every request |
| `MUSE2API_SYSTEM_PROMPT` | session-isolation text | System prompt content; use `\n` for line breaks |
| `MUSE2API_MEDIA_CLEANUP_ENABLED` | `1` | Enable scheduled media cleanup |
| `MUSE2API_MEDIA_RETENTION_DAYS` | `1` | Retention in days; `0` disables age-based deletion |
| `MUSE2API_MEDIA_MAX_GB` | `1` | Total media cap; `0` disables the cap |
| `MUSE2API_CLEANUP_INTERVAL_MINUTES` | `60` | Cleanup interval (minimum 5) |
| `MUSE2API_DEBUG_RETENTION_DAYS` | `7` | Retention for `debug-*.json` at startup |
| `MUSE2API_REF_IMAGE_LOCAL_FILES` | `0` | Allow local file paths as references |
| `MUSE2API_REF_IMAGE_PRIVATE_FETCH` | `0` | Allow private-network reference URLs |
| `MUSE2API_KEEPALIVE_DISABLED_ACCOUNTS` | `0` | Keep disabled accounts alive in the background |
| `MUSE2API_TOOL_PROTOCOL` | `0` | Describe `tools` in the prompt for emulated function calling (off by default) |

Reference media limits match the muse.ai web app: 10 items per request, 40 MiB each, 100 MiB total; images `jpg/png/gif/webp/heic/heif/avif`, videos `mp4/mov/webm/3gp` up to 120 seconds, audio `mp3/wav/m4a/ogg/flac/aac/opus`, documents `pdf/doc/docx`.

---

## Tests

The regression suites run offline against a temporary Chromium profile and synthetic DOM. They do not touch muse.ai or consume generation quota.

```bash
python tests/test_media_selection.py
python tests/test_async_images.py
python tests/test_session_health.py
python tests/test_ref_image_safety.py
```

---

## Security

- All data, including account cookies, the task queue, and generated media, stays in the local `data/` directory. There is no telemetry and no third-party relay.
- Keep `data/accounts.json` private. API responses and logs never echo proxy passwords.
- This project is for technical study, automation research, and testing. Do not use it to violate the muse.ai terms of service or any applicable law.

---

## License

Released under the [MIT License](LICENSE).

---

## Reference

This repository is a fork of the original project:

- **Original repository:** [github.com/czg86389-hub/muse2api](https://github.com/czg86389-hub/muse2api)
- **This fork:** [github.com/nguyenduclong-ict/muse2api](https://github.com/nguyenduclong-ict/muse2api)

Thanks to the original author and all upstream contributors for the foundation this build extends.
