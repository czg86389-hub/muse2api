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

---

> ### 🔧 关于本仓库（先读这段）
>
> 这是 **[czg86389-hub/muse2api](https://github.com/czg86389-hub/muse2api)** 的一个**修复分支**。
> 主体功能与上游 v1.5.2 一致，额外叠加了 **11 项并发 / 稳定性 / 安全修复**
> （详见下方 [「本分支相对上游的修复」](#-本分支相对上游的修复)）。
>
> - 🔀 修复已向上游提交 **PR #4**（等待合并）：<https://github.com/czg86389-hub/muse2api/pull/4>
> - ✅ **想要更稳的版本** → 直接用本仓库
> - 🔄 **想跟官方主线** → 用上游仓库 `czg86389-hub/muse2api`
> - 📌 上游合并 PR 后，本分支会回归上游，不再单独维护

---

将 **[muse.ai](https://muse.ai/)** 网页端的前沿多模态能力逆向工程封装为标准的 **OpenAI 兼容 RESTful API**。通过无头浏览器 CDP 协议穿透、热备 WebSocket 隧道复用与动态 Session 管理，原生支持文本对话（2~3 秒级流式首字响应）、文生图、文生图编辑、文生视频、首帧图生视频，并提供多账号池亲和轮转、全自动 48 小时会话续期与云端 VM 唤醒保活，以及配套 Chrome 一键导号扩展。

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
  - 原生对接 Muse 顶配视频生成模型，可传递 5 秒 / 6 秒 / 8 秒 / 10 秒的时长请求（实际成片时长以 Muse 上游输出为准）及 `9:16` 竖屏 / `16:9` 横屏视频生成。
  - 支持上传首帧参考图（Data URL / HTTP URL）进行严格首帧图生视频创作。
  - 异步任务架构（`/v1/videos` 创建任务 + `/v1/videos/{task_id}` 状态轮询）。
  - 内置媒体资源服务 `/v1/media/{filename}`，自动持久化存储生成的 MP4 / WebP 资源。
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
   git clone https://github.com/yys9253462-gif/muse2api.git
   cd muse2api
   ```
   > 想用上游官方版本，把地址换成 `https://github.com/czg86389-hub/muse2api.git` 即可
   > （但会缺少本分支的 11 项修复）。

2. **配置环境变量（可选）**：
   ```bash
   cp .env.example .env
   # 按需编辑 .env，建议修改 MUSE2API_KEY 为你自己的管理密钥
   ```

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

## 🧩 Chrome 扩展导号（零门槛）

项目内置了专用 Chrome 导号扩展（目录位于 `extension/`），告别复杂的 F12 Cookie 提取：

1. 打开 Chrome / Edge 浏览器，访问 `chrome://extensions`，开启右上角的 **「开发者模式」**。
2. 点击 **「加载已解压的扩展程序」**，选择本项目中的 `extension` 目录。
3. 在该浏览器中登录 [muse.ai](https://muse.ai/) 至聊天主界面。
4. 点击浏览器右上角插件图标，填入你的服务地址（例如 `http://1.2.3.4:18610`）和 `MUSE2API_KEY`。
5. 点击 **「读取并导入」**，秒级同步入库！

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

## 📄 开源许可证

本项目基于 [MIT License](LICENSE) 开源发布。

---

## 🔧 本分支相对上游的修复

本分支基于上游 **v1.5.2**（`9164bd5`），在其之上叠加了 **11 项缺陷修复**（提交 `7eb983d`）。
所有修复都来自对运行中服务的黑盒压测与故障注入，**已向上游提交 [PR #4](https://github.com/czg86389-hub/muse2api/pull/4)**。

> 与上游 v1.5.2 是**互补**关系：上游 v1.5.2 改的是图片链路（幂等入队、524 规避、
> 浏览器清理串行化），本分支改的是并发调度、CDP 通信、参数校验与鉴权。两者不重复。

### 一、并发与调度（新增 `scheduler.py`）

上游用「裸抢锁」串行化生成，存在两个致命问题，本分支改为 **FIFO 队列 + 单工作线程**：

| 问题 | 表现 | 修复 |
|---|---|---|
| **排队任务被饿死** | 并发提交多个任务时，后来的可能先抢到锁，先来的无限期等待 | `queue.Queue` 严格先来先服务，`/admin/scheduler` 可见 `waited` 排队时长 |
| **锁泄漏导致队列冻结** | 任务异常时锁未释放，之后**所有**生成请求永久挂起（全站静默停摆） | 由调度器统一持有/释放锁，异常路径也保证释放 |

- **执行超时看门狗**：单任务执行超过 `video_timeout + 300s`（默认 900s）即强制重启浏览器中断，
  不再无限期挂起。语义上让位给 engine 自身超时，不会误杀正常慢任务。
- **排队超时**：任务在队列中等待超过 900s 即判超时返回，不占用客户端连接。
- **观测端点** `GET /admin/scheduler`：返回 `submitted / completed / failed / timeout /
  rejected / queue_size / running / worker_alive`，便于排障。

### 二、CDP 通信层（重写 `cdp.py`）

上游的 `send()` 是「发一条 → 阻塞等结果」的同步模型。当某个任务卡在一次 `recv()` 上时，
**该连接上后续所有请求都会超时**。

本分支重写为**单后台读循环架构**：一个 reader 线程统一分发所有响应，
`send()` 只负责投递 + 等待自己的 id，并带写超时；读线程若死亡会唤醒全部等待者并明确报错。
一个任务卡死不再拖垮其他请求。

### 三、参数的入参校验

上游对非法参数「照单全收」，往往在生成阶段才失败（用户白等几分钟）：

- `validate_video_duration()` —— 视频时长白名单校验
- `validate_size()` —— 图片/视频尺寸与宽高比校验（超出 64~8192 直接拒绝）
- `validate_reference_image()` —— 参考图仅接受 `http(s)` URL / data URI / base64，其余入口即拒

### 四、鉴权加固

- **修复 405 鉴权绕过**：Starlette 对不支持的 HTTP 方法会在 **FastAPI 依赖注入之前**返回 405，
  导致未授权请求也能探测到接口是否存在。本分支加了一层 `_guard_method_not_allowed`
  中间件，在任何 405 之前先校验 Bearer token。

### 五、更诚实的进度语义

- **移除伪造的 92% 进度**：上游对运行中的任务用 `min(92, 20 + elapsed * 1.1)` 计算进度，
  于是一个**早已卡死的任务**会永远对外显示「92%，快好了」，客户端被无限期欺骗。
  本分支队列中的任务如实返回 `{"status": "queued", "progress": 0, "stage": "queued"}`。
- **新增 `stalled` 状态**：任务超过 120s 无任何进展（基于 `updated_at` 空闲时长，而非 progress 值）
  才判定为疑似卡死，返回 `status: "stalled"`。**不会误伤慢任务**——正常长任务会持续刷新 `updated_at`。
- 新增 `stage` 字段（`queued` / `rendering` / `done` / `failed` / `stalled`），是对原 `status` 的补充，
  老客户端读 `status` 不受影响。

### 六、前端 DOM 解析修正

- 回复计数过滤**空骨架气泡**（页面上存在同类的占位元素），修正回复数虚高。
- `reset_thread` 的 `bubbleCount` 只统计主聊天区，未就绪时显式报错而非静默继续。

### 兼容性

- 所有改动都在原语义上**收紧**（原本被错误接受的输入现在被拒），**合法请求行为不变**。
- 新增 `stage` 字段是对 `status` 的补充，不是替换。
- `scheduler.py` 为新增文件，`Dockerfile` 已同步 `COPY` 行；用 Docker 部署无需额外操作。

### 同步上游

若上游合入了 PR #4 或发布了新版本，本分支同步方式：

```bash
git remote add upstream https://github.com/czg86389-hub/muse2api.git
git fetch upstream
git merge upstream/main          # 或 git rebase upstream/main
```

---

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
