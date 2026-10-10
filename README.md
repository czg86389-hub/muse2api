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
  - 支持 `1:1`、`16:9`、`9:16`、`4:3`、`3:4` 等多画幅生成，支持参考图直传与去水印纯净输出。
  - 支持 `url` 直链或 `b64_json` 两种返回格式，内置纯文本拒答秒级快速检测，杜绝队列死锁。
  - 图生图仅返回本次新生成的媒体：排除上传预览与历史附件，下载绑定选中的结果；参考图上传或提示词发送未确认时明确报错，不再静默返回原图。
- 🎬 **文生视频 / 图生视频（Videos）**
  - 异步任务：`POST /v1/videos` 创建，`GET /v1/videos/{task_id}` 轮询，完成状态是 `completed`。
  - `duration` 不传时按 6 秒请求，服务端不截断秒数，成片时长以 Muse 输出为准。`16:9` 与 `9:16` 由 `aspect_ratio` 或 `size` 指定。
  - 参考图最多 10 张，字段可用 `reference_images`、`images`、`image_urls`，也兼容单张 `reference_image` / `image_url` / `image`。
  - 成品上传到图床后返回公网地址。对接说明见 [docs/video-api.md](docs/video-api.md)。
- 🔄 **多账号池与热连接亲和调度**
  - 支持导入无上限的 Muse 账号矩阵。
  - 基于热连接亲和（Warm-Tab Affinity）与 LRU 策略智能分发，兼顾 2 秒级极速响应与多号均衡消耗。
  - 遇到单号额度耗尽或会话异常时，自动标记并 0 秒无感故障转移至备用健康账号。
- 🛡️ **48 小时会话全自动续期与云端 VM 保活**
  - 独创后台心跳协程，直连 `/api/session` 自动续签 `hatch_vml`（+48h）与 `hatch_sess`（+30d），并自动调用 `/api/hatch/vm/wake` 保持云端工作区 VM 热备。
  - 彻底解决 Meta Cookie 静态 48 小时到期与 VM 休眠断连难题，无需频繁重新登录。
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
       client_max_body_size 64M;     # 支持参考图大文件上传
       proxy_set_header Host $host;
       proxy_set_header X-Real-IP $remote_addr;
       proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
   }
   ```

---

## 🧩 账号导入（Chrome 扩展 / 油猴脚本）

### 方式 A：Chrome / Edge 扩展
项目内置了专用 Chrome 导号扩展（目录位于 `extension/`），告别复杂的 F12 Cookie 提取：

1. 打开 Chrome / Edge 浏览器，访问 `chrome://extensions`，开启右上角的 **「开发者模式」**。
2. 点击 **「加载已解压的扩展程序」**，选择本项目中的 `extension` 目录。
3. 在该浏览器中登录 [muse.ai](https://muse.ai/) 至聊天主界面。
4. 点击浏览器右上角插件图标，填入你的服务地址（例如 `http://1.2.3.4:18610`）和 `MUSE2API_KEY`。
5. 点击 **「读取并导入」**，秒级同步入库！

### 方式 B：Tampermonkey 油猴脚本（跨浏览器免安装扩展）
对于不想开启开发者模式或使用 Firefox / Safari 的用户，提供了油猴脚本（位于 `tools/muse2api_cookie_importer.user.js`）：
1. 在浏览器安装 Tampermonkey 插件。
2. 新建脚本并将 `tools/muse2api_cookie_importer.user.js` 内容复制粘贴保存。
3. 打开 [muse.ai](https://muse.ai/) 网页，右下角将出现 **「⚡ 导入到 muse2api」** 悬浮按钮，右键即可配置服务地址与 Key，左键一键推送入库并自动复制剪贴板兜底。

---

## 📖 API 调用示例

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

### 3. 文生视频 / 图生视频（Videos）

完整字段、参考图、排队和错误码见 [docs/video-api.md](docs/video-api.md)。视频接口始终异步：创建返回 `queued`，轮询到 `completed` 后读取 `url`。

```bash
curl -X POST "https://muse.secure-skill.com/v1/videos" \
  -H "Authorization: Bearer m2a_your_secret_key" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"金色的枫叶在微风中轻盈飘落","duration":6,"aspect_ratio":"16:9"}'

curl "https://muse.secure-skill.com/v1/videos/task_xyz789" \
  -H "Authorization: Bearer m2a_your_secret_key"
```

完成时 `status` 为 `completed`，`url` 是图床上的 mp4 地址。

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
| `MUSE2API_IMAGE_TIMEOUT` | `240` | 生图超时上限（秒） |
| `MUSE2API_VIDEO_TIMEOUT` | `600` | 生视频超时上限（秒） |
| `MUSE2API_CHAT_TIMEOUT` | `300` | 对话生成超时上限（秒） |

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
- 🌟 **[@CarloCPP](https://github.com/CarloCPP)** (PR [#6](https://github.com/czg86389-hub/muse2api/pull/6)) —— 贡献 Tampermonkey 油猴一键导号脚本及已禁用账号自动保活配置
- 🌟 **[@djs-91](https://github.com/djs-91)** (PR [#9](https://github.com/czg86389-hub/muse2api/pull/9)) —— 修复未配置 `MUSE2API_PUBLIC_BASE` 时 Base URL 坍缩导致管理页地址解析异常
- 🌟 **[@toby-bridges](https://github.com/toby-bridges)** (PR [#8](https://github.com/czg86389-hub/muse2api/pull/8)) —— 适配 muse.ai 现网中文 UI、修复附件上传确认与 Stop 状态误判、强化视频成品与封面防误选

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

1. `POST /v1/images/tasks`：JSON 与 `/v1/images/generations` 相同，可用 `reference_image` 传入图生图参考；建议添加稳定的 `Idempotency-Key` 请求头。
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
