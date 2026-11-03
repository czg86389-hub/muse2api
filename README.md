# MUSE2API

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-blue?logo=python" alt="Python Version" />
  <img src="https://img.shields.io/badge/FastAPI-0.115%2B-009688?logo=fastapi" alt="FastAPI" />
  <img src="https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker" alt="Docker" />
  <img src="https://img.shields.io/badge/API-OpenAI%20Compatible-green" alt="OpenAI API Compatible" />
  <img src="https://img.shields.io/badge/License-MIT-orange" alt="License" />
  <a href="https://linux.do/" target="_blank"><img src="https://img.shields.io/badge/Community-LINUX%20DO-111827?logo=linux&logoColor=white" alt="LINUX DO" /></a>
</p>

<p align="center">
  🤝 本开源项目已链接并认可 <b><a href="https://linux.do/" target="_blank">LINUX DO 社区 (https://linux.do/)</a></b> —— 新的理想型社区（真诚、友善、团结、专业）
</p>

将 **[muse.ai](https://muse.ai/)** 网页端的前沿多模态能力逆向工程封装为标准的 **OpenAI 兼容 RESTful API**。通过无头浏览器 CDP 协议穿透、热备 WebSocket 隧道复用与动态 Session 管理，原生支持文本对话（2~3 秒级流式首字响应）、文生图、图生图编辑、文生视频、首帧图生视频，并提供多账号池亲和轮转、全自动 48 小时会话续期与云端 VM 唤醒保活，以及配套 Chrome 一键导号扩展。

---

## 🌟 核心特性

- 💬 **标准对话接口（Chat Completions & Responses API）**
  - 完全兼容 `/v1/chat/completions` 与新版 Codex 默认使用的 `/v1/responses` 标准协议。
  - 原生支持 SSE 流式打字机输出（`stream=True`）与同步完整返回，内置热标签页与 Noise WebSocket 隧道亲和复用，**连续对话首字延迟仅需 2~3 秒**。
  - 支持多轮对话上下文、System Prompt 设定。
  - 内置模型智能别名映射，`gpt-4o`、`gpt-5`、`claude-sonnet-4`、`deepseek-chat` 等常用模型名自动路由。
- 🎨 **高质量生图与图像编辑（Images Generations & Edits）**
  - 完全兼容 `/v1/images/generations`（文生图）与 `/v1/images/edits`（图生图/参考图编辑）接口。
  - 支持 `1:1`、`16:9`、`9:16`、`4:3`、`3:4` 等多画幅生成，支持单图或多图参考直传与去水印纯净输出。
  - 支持 `url` 直链或 `b64_json` 两种返回格式，内置纯文本拒答秒级快速检测，杜绝队列死锁。
  - 图生图仅返回本次新生成的媒体：排除上传预览与历史附件，下载绑定选中的结果；参考图上传或提示词发送未确认时明确报错，不再静默返回原图。
- 🎬 **文生视频 / 图生视频（Videos）**
  - 原生对接 Muse 顶配视频生成模型，可传递 5 秒 / 6 秒 / 8 秒 / 10 秒的时长请求（实际成片时长以 Muse 上游输出为准）及 `9:16` 竖屏 / `16:9` 横屏视频生成。
  - 支持上传单张首帧参考图，或通过 `images` 数组上传多张内容与风格参考图；多图模式以第一张作为主要起始画面。
  - 异步任务架构（`/v1/videos` 创建任务 + `/v1/videos/{task_id}` 状态轮询）。
  - 内置媒体资源服务 `/v1/media/{filename}`，自动持久化存储生成的 MP4 / WebP 资源。
- ⚡ **真多账号并行并发（Multi-Account Parallel Execution）**
  - 基于 Chromium 原生 `Target.createBrowserContext` 隐身隔离容器，多任务在独立沙箱中并发执行。
  - 多账号同时生图、生视频、流式对话零互相阻塞，Cookie 与 Session 100% 隔离绝不串扰。
  - 内置智能账号冲突调度器，同账号自动互斥防踩踏，多账号自动并行跑满。
  - 可通过 `MUSE2API_CONCURRENCY` 随机器性能灵活调大并发上限（默认 2 并发）。
- 🔄 **多账号池与热连接亲和调度**
  - 支持导入无上限的 Muse 账号矩阵。
  - 基于热连接亲和（Warm-Tab Affinity）与 LRU 策略智能分发，兼顾 2 秒级极速响应与多号均衡消耗。
  - 遇到单号额度耗尽或会话异常时，自动标记并 0 秒无感故障转移至备用健康账号。
- 🛡️ **会话状态检测与云端 VM 保活**
  - 后台请求 `/api/session` 检查会话，并按需调用 `/api/hatch/vm/wake` 唤醒工作区；配置账号代理后，这些请求与浏览器使用同一出口。
  - 只保存上游实际返回的 Cookie 和到期时间，不会因检测成功而虚增有效期。上游未续签、会话被撤销或账号受限时，仍需在官网确认并重新导入。
- 🌐 **账号独立 HTTP / SOCKS5 代理**
  - 每个账号可配置不同的 HTTP 或 SOCKS5 代理及认证信息，并发任务的浏览器上下文与会话请求按账号隔离；代理失败不会静默回退直连。
- 🧩 **配套 Chrome 一键导号扩展**
  - 无需手工 F12 抓包，点一下扩展图标即刻将当前浏览器登录态（含 `HttpOnly` 核心 Cookie 与真实过期时间）提取并安全推送至账号池。
- 🖥️ **现代化深色运维面板（Web Console）与实时在线热升级**
  - 内置开箱即用的 Web UI，支持实时查看服务健康度、账号池额度与状态、一键全池保活、任务进度回放、媒体库管理与在线接口调试。
  - **全网节点实时更新广播与一键升级**：当官方 GitHub 仓库发布新版本或修复时，所有已部署节点的管理后台顶部会自动弹出更新通知横幅，点击 **「⚡ 一键在线升级并重启」** 即可自动拉取最新代码并平滑重启（自动保留本地 `.env` 配置与账号数据）。

---

## 🚀 极速部署

### 方式一：Docker Compose（推荐，一行命令开箱即用）

1. **克隆代码并进入目录**：
   ```bash
   git clone https://github.com/czg86389-hub/muse2api.git
   cd muse2api
   ```

2. **配置环境变量（可选）**：
   ```bash
   cp .env.example .env
   # 按需编辑 .env，建议修改 MUSE2API_KEY 为你自己的管理密钥
   ```

   Docker Compose 会读取 `.env` 中的 `MUSE2API_KEY`，不会再被 Compose 文件中的示例值覆盖。

3. **启动容器**：
   ```bash
   docker compose up -d
   ```

4. **访问管理面板**：
   打开浏览器访问：`http://<你的服务器IP>:18610/admin?key=<你的MUSE2API_KEY>`

---

### 方式二：裸机 / VPS 部署（Ubuntu / Debian）

1. **安装系统依赖与 Chromium**：
   ```bash
   sudo apt-get update
   sudo apt-get install -y chromium fonts-wqy-zenhei python3 python3-pip python3-venv
   ```

2. **配置 Python 虚拟环境**：
   ```bash
   cd /opt/muse2api
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. **配置 systemd 服务**：
   ```bash
   sudo cp deploy/muse2api.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now muse2api
   ```

4. **Nginx 反向代理配置（重要：防止 502 超时与流式卡顿）**：
   如使用 Nginx / 宝塔 / 1Panel 反代，务必将 `proxy_read_timeout` 调大至 `600s` 并关闭 `proxy_buffering`（详见 `deploy/nginx.example.conf`）：
   ```nginx
   location / {
       proxy_pass http://127.0.0.1:18610;
       proxy_read_timeout 600s;      # 避免生图/视频耗时较长被 Nginx 报 502 Bad Gateway
       proxy_send_timeout 600s;
       proxy_buffering off;          # 保证 SSE 对话流式打字机 0 延迟吐字
       client_max_body_size 200M;    # 支持视频/音频等大参考素材上传
       proxy_set_header Host $host;
       proxy_set_header X-Real-IP $remote_addr;
       proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
   }
   ```

---

## 🧩 账号导入（Chrome 扩展 / 油猴脚本）

> **v1.6.6 用户请务必手动升级插件：Chrome/Edge 扩展最低 1.0.2，油猴脚本最低 1.1.2。**
> 服务端更新不会自动替换已加载的扩展。下载本 Release 的 ZIP，覆盖原扩展目录文件，在 chrome://extensions / edge://extensions 点击“重新加载”；不同个人资料分别升级。弹窗标题旁应显示 **v1.0.2**。
> [下载扩展 1.0.2](https://github.com/czg86389-hub/muse2api/releases/download/v1.6.6/muse2api-chrome-extension-v1.6.6.zip)。只有 hatch_sess 是导入必要项；hatch_vml 和 hatch_gw 缺失不能据此判定未登录。重新导入后再点击账号“测试”。
>
> **Manual extension upgrade required:** extension 1.0.2+ / userscript 1.1.2+. A server update does not upgrade unpacked extensions. Replace files in the original folder and click Reload, separately for every browser profile.


### 方式 A：Chrome / Edge 扩展
项目内置了专用 Chrome 导号扩展（目录位于 `extension/`），告别复杂的 F12 Cookie 提取：

1. 打开 Chrome / Edge 浏览器，访问 `chrome://extensions`，开启右上角的 **「开发者模式」**。
2. 点击 **「加载已解压的扩展程序」**，选择本项目中的 `extension` 目录。
3. 在该浏览器中登录 [muse.ai](https://muse.ai/) 至聊天主界面。
4. 点击浏览器右上角插件图标，填入你的服务地址（例如 `http://1.2.3.4:18610`）和 `MUSE2API_KEY`。
5. 点击 **「读取并导入」**，秒级同步入库！

### 方式 B：Tampermonkey 油猴脚本（跨浏览器免安装扩展）
对于不想开启开发者模式或使用 Firefox / Safari 的用户，推荐直接使用油猴脚本一键安装：

👉 **[⚡ 点击一键安装油猴脚本 (Raw UserJS)](https://raw.githubusercontent.com/czg86389-hub/muse2api/main/tools/muse2api_cookie_importer.user.js)**

1. 在浏览器安装 [Tampermonkey](https://www.tampermonkey.net/) 插件。
2. 点击上方 **「⚡ 点击一键安装油猴脚本」** 链接，Tampermonkey 将自动弹出安装窗口，点击「安装」即可（无需手动复制粘贴代码）。
3. 打开 [muse.ai](https://muse.ai/) 网页，右下角将出现 **「⚡」** 悬浮图标，点击即可展开配置面板并一键推送入库（亦支持自动复制剪贴板兜底）。

---

## 📖 API 调用示例

### 账号独立代理 / Per-account proxy

在账号池点击账号旁的 **代理 / Proxy**：

1. 选择 **指定代理 / Use a proxy**，填写服务器、用户名与密码；不需要认证时留空用户名和密码。
2. 保存后重新打开窗口，点击 **测试已保存代理 / Test saved proxy**。结果分别显示代理连通性、出口 IP 与 Muse 会话状态。
3. 继续使用账号的 **测试 / Test** 按钮，确认浏览器能加载聊天输入框。会话验证成功不保证上游一定允许某次图片或视频生成。

支持 HTTP 代理端点（可选 Basic 认证）与 SOCKS5 代理端点（可选用户名/密码认证），例如 `http://proxy.example.com:8080`、`socks5://proxy.example.com:1080` 或 `socks5h://proxy.example.com:1080`。SOCKS5 的目标主机名经代理解析；HTTPS 网站照常支持。**HTTPS 代理端点、VLESS、VMess 与订阅链接暂不支持直接填写**；使用 Clash/Mihomo 等工具时，应填部署服务器可访问的 HTTP 或 SOCKS5 代理入口。`127.0.0.1:7890` 指的是部署服务器自身，不是导入 Cookie 的电脑。没有配置代理的旧账号保持原有网络行为。

聊天、生图、视频、浏览器下载、账号检测、额度查询、会话续签与 VM 唤醒使用账号代理。代理断线、认证失败或浏览器上下文创建失败会报告错误，**不会静默回退直连**。账号切换同时切换代理。外部参考图在账号选取前按现有安全策略预取，继续执行公网地址、重定向与 DNS 重绑定防护，不承诺任意参考图 URL 都通过账号代理。

账号忙碌时修改代理会返回 HTTP 409，请等待当前任务结束。修改后，下次请求会重建连接，不复用旧节点。代理连通性与账号会话是不同状态：HTTP 407、连接超时不会直接把 Cookie 标成失效；代理测试也不会覆盖账号已有的认证结果。

代理密码与 Cookie 一起保存在本机 `data/accounts.json`，应保护此目录及备份。接口、日志和页面不回显密码。修改时地址与用户名不变，密码留空表示保留；改变地址或用户名时应重新输入认证密码。选择 **默认网络 / Default network** 或 **移除代理 / Remove proxy** 清除配置并恢复默认网络。

管理 API 示例（需鉴权）：

```http
PATCH /admin/accounts/{account_id}
Content-Type: application/json
Authorization: Bearer <MUSE2API_KEY>

{
  "proxy": {
    "server": "http://proxy.example.com:8080",
    "username": "your-proxy-user",
    "password": "your-proxy-password"
  }
}
```

省略 `proxy` 不修改设置；提交 `{"proxy": null}` 移除代理。`POST /admin/accounts` 也接受同一 `proxy` 对象，包括为本次批量导入设置相同代理。响应仅包含服务器、用户名和 `has_password`，不返回密码。

```http
POST /admin/accounts/{account_id}/proxy/test
Authorization: Bearer <MUSE2API_KEY>
```

该测试通过已保存代理访问无 Cookie 的 HTTPS 出口检测，再验证 Muse 会话；返回的 `proxy_reachable`、`exit_ip`、`session_ok` 与 `workspace_status` 分别表示各层结果。没有配置代理时返回 400，账号忙碌时返回 409。此操作不生成内容，也不消耗图片/视频生成额度。

**English:** Each account can use its own HTTP proxy with optional Basic authentication or a SOCKS5 proxy with optional username/password authentication. Select **Use a proxy**, save the server and credentials, then reopen the dialog and choose **Test saved proxy**. Both `socks5://` and `socks5h://` proxy origins are accepted, and target hostnames are resolved by the proxy. HTTPS sites are supported; HTTPS proxy endpoints, VLESS/VMess and subscription URLs are not supported directly. The proxy address must be reachable from the deployment server. A proxy failure never falls back to direct access. Unconfigured accounts retain their existing network behavior. Active accounts cannot change proxies (HTTP 409); the next request recreates the context after a saved change. Keep the password blank only to preserve it with an unchanged server and username. Neither API responses nor logs expose it. The proxy test reports connectivity, egress IP and Muse session status separately; it does not prove generation capability or replace the account's browser test.

---

所有受保护接口均需在 Header 中携带：
```http
Authorization: Bearer <你的MUSE2API_KEY>
```

### 1. 对话补全（Chat Completions）

```bash
curl -X POST "http://localhost:18610/v1/chat/completions" \
  -H "Authorization: Bearer m2a_your_secret_key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "muse-spark",
    "messages": [
      {"role": "user", "content": "写一首关于赛博朋克与霓虹夜雨的七言绝句"}
    ],
    "stream": false
  }'
```

### 2. 文生图（Image Generation）

```bash
curl -X POST "http://localhost:18610/v1/images/generations" \
  -H "Authorization: Bearer m2a_your_secret_key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "muse-image",
    "prompt": "一只穿着反重力宇航服的机械柴犬，电影级光影，8k分辨率",
    "size": "16:9",
    "response_format": "url"
  }'
```

**响应示例**：
```json
{
  "created": 1790148495,
  "data": [
    {
      "revised_prompt": "一只穿着反重力宇航服的机械柴犬，电影级光影，8k分辨率",
      "url": "http://localhost:18610/v1/media/img_abc123.webp",
      "kind": "image",
      "bytes": 54210
    }
  ]
}
```

多图参考使用 `images` 数组（最多 10 个素材；单个不超过 40 MiB、合计不超过 100 MiB，详见下方「参考素材」）；服务会按数组顺序逐张上传，并在全部附件得到网页确认后才发送提示词。`images` 不可与 `image` / `reference_image` 等旧版单图字段混用：

```bash
curl -X POST "http://localhost:18610/v1/images/generations" \
  -H "Authorization: Bearer m2a_your_secret_key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "muse-image",
    "prompt": "结合第一张图的主体与第二张图的配色生成海报",
    "images": [
      {"url": "https://example.com/subject.png"},
      {"url": "https://example.com/style.png"}
    ],
    "response_format": "url"
  }'
```

`/v1/images/edits` 的 multipart 请求可以重复提交同名的 `image` 字段：

```bash
curl -X POST "http://localhost:18610/v1/images/edits" \
  -H "Authorization: Bearer m2a_your_secret_key" \
  -F "prompt=结合两张参考图生成一张新图" \
  -F "image=@subject.png" \
  -F "image=@style.png"
```

### 3. 文生视频 / 图生视频（Videos）

- **第一步：创建生成任务**
  ```bash
  curl -X POST "http://localhost:18610/v1/videos" \
    -H "Authorization: Bearer m2a_your_secret_key" \
    -H "Content-Type: application/json" \
    -d '{
      "prompt": "金色的枫叶在微风中轻盈飘落，阳光穿透树梢",
      "duration": 5,
      "size": "16:9"
    }'
  ```
  返回任务 ID：`{"id": "task_xyz789", "status": "queued"}`

  多图参考视频在同一个请求中使用有序的 `images` 数组；这里支持的是多张图片作为视频参考，不支持上传视频文件作为参考：
  ```bash
  curl -X POST "http://localhost:18610/v1/videos" \
    -H "Authorization: Bearer m2a_your_secret_key" \
    -H "Content-Type: application/json" \
    -d '{
      "prompt": "让第一张图的主体在第二张图的场景与配色中运动",
      "images": [
        {"url": "https://example.com/start.png"},
        {"url": "https://example.com/style.png"}
      ],
      "duration": 6,
      "size": "16:9"
    }'
  ```

- **第二步：轮询任务进度**
  ```bash
  curl "http://localhost:18610/v1/videos/task_xyz789" \
    -H "Authorization: Bearer m2a_your_secret_key"
  ```
  完成时返回：
  ```json
  {
    "id": "task_xyz789",
    "status": "succeeded",
    "progress": 100,
    "result": {
      "url": "http://localhost:18610/v1/media/vid_xyz789.mp4"
    }
  }
  ```

---

## 🎞️ 参考素材：图片 / 视频 / 音频 / 文档

生图、图像编辑、图片任务与视频接口都可以附带参考素材，限制与 muse.ai 网页端一致：

| 项目 | 限制 |
|---|---|
| 数量 | 每次最多 10 个（所有字段合计） |
| 单个大小 | 40 MiB；合计 100 MiB |
| 图片 | jpg / png / gif / webp / heic / heif / avif |
| 视频 | mp4 / mov / webm / 3gp，时长不超过 120 秒（mp4/mov 会预先校验） |
| 音频 | mp3 / wav / m4a / ogg / flac / aac / opus |
| 文档 | pdf / doc / docx |

- JSON 字段：`images`（可混放任意素材）、`videos`、`audios`、`files`，按此顺序依次上传；每项可以是 URL、Data URL、Base64，或 `{"url": ...}` / `{"video_url": ...}` / `{"audio_url": ...}` 对象。
- 类型取 Data URL / 响应头 / multipart 声明的 MIME；声明为空或 `application/octet-stream` 时按文件头识别。
- `/v1/images/edits` 的 multipart 可重复提交 `image`、`video`、`audio`、`file` 字段。
- 视频任务若第一个素材是图片，以它作为主要起始画面；成品绝不会是本次上传的素材本身（按内容哈希排除）。

```bash
curl -X POST "http://localhost:18610/v1/videos"   -H "Authorization: Bearer m2a_your_secret_key"   -H "Content-Type: application/json"   -d '{
    "prompt": "参考视频的运镜节奏，配合音频的情绪，生成一段新的城市夜景",
    "videos": [{"url": "https://example.com/ref.mp4"}],
    "audios": [{"url": "https://example.com/bgm.mp3"}],
    "duration": 8,
    "size": "16:9"
  }'
```

Nginx 反代需把 `client_max_body_size` 调到 200M 左右，否则大素材会被 413 拒绝。

---

## ⚙️ 环境变量配置一览

| 变量名 | 默认值 | 作用与说明 |
|---|---|---|
| `MUSE2API_KEY` | 自动生成 | 管理控制台与 API 鉴权密钥（以 `m2a_` 开头） |
| `MUSE2API_HOST` | `127.0.0.1` | 监听 IP（Docker 内建议 `0.0.0.0`） |
| `MUSE2API_PORT` | `18610` | 服务运行端口 |
| `MUSE2API_PUBLIC_BASE` | `空` | 对外完整 URL 前缀，留空时前端自动识别访问来源 |
| `MUSE2API_CHROMIUM` | `chromium` | 浏览器可执行程序完整路径 |
| `MUSE2API_CDP_PORT` | `19210` | 内部 CDP 调试通信端口 |
| `MUSE2API_CONCURRENCY` | `2` | 最大并发生成任务数（支持随服务器资源自由调高，默认 2） |
| `MUSE2API_IMAGE_TIMEOUT` | `240` | 生图超时上限（秒） |
| `MUSE2API_VIDEO_TIMEOUT` | `600` | 生视频超时上限（秒） |
| `MUSE2API_CHAT_TIMEOUT` | `300` | 对话生成超时上限（秒） |
| `MUSE2API_REF_IMAGE_LOCAL_FILES` | `0` | 默认禁止读取服务器本地路径；仅可信单用户部署可显式设为 `1` 恢复 |
| `MUSE2API_REF_IMAGE_PRIVATE_FETCH` | `0` | 默认仅允许公网 URL，并逐跳检查 DNS/重定向、固定已检查 IP、不使用环境代理；显式设为 `1` 才允许私网/本机 URL |

两个开关默认均为 `0`：所有生图、图像编辑、图片任务及视频接口都禁止服务器本地路径和私网/本机 URL。Data URL、有效 Base64 和公网 URL 正常使用。只有确认调用方可信且确有需要时，才显式设为 `1` 恢复对应旧能力。
策略拒绝、下载失败、MIME 或大小不合规都会在提交任务前返回 HTTP 400，不会创建一个随后才失败的后台任务。

参考图安全回归检查（模拟 DNS 和 HTTP，无真实网络或账号）：

```bash
python tests/test_ref_image_safety.py
```

---

## 🔒 安全与隐私声明

- **零数据外泄**：本软件全部数据（包括账号凭据、任务队列、媒体文件）均持久化在本地 `data/` 目录中，不依赖任何第三方遥测或外部中转服务。
- **开源合规**：本项目仅供技术交流、系统自动化运维研究与自动化测试。请勿将本项目用于违反 Meta 平台服务条款或任何国家法律法规之用途。

---

## 🤝 社区认可与友情链接

本项目已链接并高度认可 **[LINUX DO 社区](https://linux.do/)**，感谢社区佬友的交流、反馈与支持：

- 🌐 **[LINUX DO 社区 (https://linux.do/)](https://linux.do/)** —— 新的理想型社区（真诚、友善、团结、专业，共建你我引以为荣之社区）

---

## 👥 贡献者致谢 (Contributors)

感谢以下开发者对 MUSE2API 的代码贡献与功能优化（按 PR 合入顺序排列）：

- 🌟 **[@cpt-kenvie](https://github.com/cpt-kenvie)** (PR [#2](https://github.com/czg86389-hub/muse2api/pull/2)) —— 修复 Docker Compose 读取 `.env` 环境变量与示例配置覆盖问题
- 🌟 **[@djs-91](https://github.com/djs-91)** (PR [#9](https://github.com/czg86389-hub/muse2api/pull/9)) —— 修复未配置 `MUSE2API_PUBLIC_BASE` 时 Base URL 坍缩导致管理页地址解析异常
- 🌟 **[@toby-bridges](https://github.com/toby-bridges)** (PR [#8](https://github.com/czg86389-hub/muse2api/pull/8)) —— 适配 muse.ai 现网中文 UI、修复附件上传确认与 Stop 状态误判、强化视频成品与封面防误选
- 🌟 **[@LF2023](https://github.com/LF2023)** (PR [#18](https://github.com/czg86389-hub/muse2api/pull/18)) —— 修复视频生成仅凭 tid 命中封面静帧问题，规范预期匹配与防误选机制
- 🌟 **[@wuliaolaozi](https://github.com/wuliaolaozi)** (PR [#27](https://github.com/czg86389-hub/muse2api/pull/27)) —— 适配中英文额度查询，修复额度读取与管理页错误提示
- 🌟 **[@CarloCPP](https://github.com/CarloCPP)** (PR [#6](https://github.com/czg86389-hub/muse2api/pull/6), [#26](https://github.com/czg86389-hub/muse2api/pull/26)) —— 贡献 Tampermonkey 油猴一键导号脚本、放宽必要 Cookie 校验至 session 凭证与一键安装
- 🌟 **[@gr219](https://github.com/gr219)** (PR [#31](https://github.com/czg86389-hub/muse2api/pull/31)) —— 为管理后台及 Chrome 扩展提供中英文国际化双语动态切换支持
- 🌟 **[@Yeadon8888](https://github.com/Yeadon8888)** (PR [#32](https://github.com/czg86389-hub/muse2api/pull/32)) —— 增加有序多参考图生图、编辑与图生视频支持
- 🌟 **[@pandalaohe](https://github.com/pandalaohe)** (PR [#24](https://github.com/czg86389-hub/muse2api/pull/24)) —— 提供参考图本地文件与私网 URL 安全策略、重定向复查和 DNS 重绑定防护
- 🌟 **[@QuartzWarrior](https://github.com/QuartzWarrior)** (PR [#25](https://github.com/czg86389-hub/muse2api/pull/25)) —— 贡献账号独立 HTTP 代理基础、认证转接、配置界面与隔离回归测试
- [@Roins-hub](https://github.com/Roins-hub)：新版 Muse 输入区兼容、视频/音频/文档参考素材（PR #40）。

欢迎大家提交 PR 与 Issue，共同把项目打磨得更强更好用！

---

## 📄 开源许可证

本项目基于 [MIT License](LICENSE) 开源发布。


## 媒体选择回归测试

修复上传参考图被误判为生成结果、下载过程被页面其他媒体覆盖的问题。
测试使用独立临时 Chromium profile 和本地合成 DOM，不访问 Muse、不读取账号、不会消耗生成额度。
安装项目依赖和 Chromium 后，从仓库根目录运行：

```bash
python tests/test_media_selection.py
```

如浏览器不在 PATH，可设置 `MUSE2API_CHROMIUM` 为可执行文件路径。
覆盖上传预览排除、结果去重、历史附件排除、精确来源下载、视频来源保持、
禁止全局下载回退，以及参考图读取失败时中止。最终图片编辑效果仍需通过真实接口验收。


## v1.5.2：反向代理/CDN 后的长耗时生图

同步 OpenAI 图片接口仍保留兼容，但 CDN/客户端可能在生成结束前截断长请求。
生产客户端应使用短提交 + 轮询，而不是延长 HTTP 超时后重复生成：

1. `POST /v1/images/tasks`：JSON 与 `/v1/images/generations` 相同，可用 `reference_image` 传入单张参考图，或用 `images` 数组传入多张参考图；建议添加稳定的 `Idempotency-Key` 请求头。
2. 接收 HTTP 202 和 `id`，每 3 秒调用 `GET /v1/images/tasks/{id}`。
3. `status=completed` 时读取 `url` 或 `data[0]`；`status=failed` 时显示 `error`。查询失败只重试查询，不要重新 POST 生图。
4. 相同幂等键及相同输入返回原任务；同键不同输入返回 409；队列满返回 429。一个进程共享一个浏览器，最多接收 8 个未结束图片任务。

原 `/v1/images/generations` JSON 和 `/v1/images/edits` JSON/multipart 也可传 `async=true`，切换到同一异步处理。默认同步行为不变。
`timeout` 为排队及生成等待预算（1–600 秒），浏览器初始化与取回另有各自超时；客户端应继续轮询任务终态。
任务元数据持久化；进程重启时尚未完成的图片任务标记失败，不会悄悄再次生成。
管理页图片接口测试也已改为任务轮询。

回归检查（不消耗真实生成额度）：

```bash
python tests/test_async_images.py
python tests/test_vm_wait.py engine.py --assert
```

修复共享浏览器异常处理在解锁后重置其他任务的竞争；不再把页面侧栏残留的
`Connecting...` / `Still sending` 文本当作当前生图在 16 秒内失败的证据。


## v1.5.3：账号状态异常与自建实例排查

保活请求来自**部署服务器的出口**，并非导出 Cookie 的浏览器。作者服务器可用，不能证明另一台服务器的网络、地区、代理配置或同一账号会话也正常。

- `/api/session HTTP 401`：上游拒绝认证。先确认该账号能在 muse.ai 官网登录，再重新导入 Cookie。
- `HTTP 403`：访问被拒绝，可能涉及账号权限、服务器出口或地区/访问限制；**不等同于 Cookie 过期**。检查完整错误和部署网络，不要反复盲目重导。
- `HTTP 429`、`5xx`、超时、非 JSON 或未返回 `assigned`：本次保活未确认，不再假报成功或覆盖上次已确认的账号状态；备注显示诊断。原状态为“未检测”或“异常”时也不会被强行改成“可用”。
- 浏览器页面加载超时也不再直接当成认证失效；只有明确认证失败才标记账号异常。
- “可用”是最近一次已确认结果，不保证本次网络可达；“剩余有效期”是 Cookie 时间/估算值，不是服务端会话有效的证明。成功检测不再凭空延长 48 小时，续期以实际返回的 Cookie 为准。

更新到 v1.5.3 并重启服务后，点击账号“测试”重新确认。旧版已写入的异常不会被无条件洗成正常。鼠标悬停备注可查看完整错误；反馈请提供 HTTP 状态、版本与部署环境，**不要公开 Cookie、API Key 或带 key 的管理页链接**。

裸机 Git 部署：`git pull --ff-only` 后重启对应服务；Docker Compose：`git pull --ff-only && docker compose up -d --build`。请先保存本地自定义修改，保留 `.env` 与 `data/`。

不使用真实账号、不消耗生成额度的回归测试：

```bash
python tests/test_session_health.py
```
# MUSE2API 1.6.1

## 更新说明（2026-10-13）

- 修复多账号额度查询串读：额度查询绑定对应账号的浏览器页面，避免不同账号之间显示错误余额和用量。
- 额度读取失败或未读到有效数据时保留旧缓存，并明确报告读取失败，避免将无效结果当成实时额度。
- 修复保活成功误报：区分唤醒请求已接受与 VM 已运行，只有复核状态为 RUNNING 才报告唤醒成功。
- 管理页显示额度查询时间和过期提示；账号测试分别反馈会话有效性与额度读取状态。
- 新增离线回归测试，覆盖账号额度隔离及 VM 唤醒状态确认。

参考素材校验说明：视频必须含有可解析且不超过 120 秒的时长信息（MP4/MOV/3GP/WebM）；
缺失或损坏的时长信息会被拒绝。PDF 页数、文档解析和账号权限仍由 Muse 上游校验，
本项目的附件确认只表示文件进入输入区，不保证上游能够读取每一种文件。
Chromium 与服务必须能访问同一份素材文件；临时素材位于浏览器 profile 目录并在任务结束后清理。
