# MUSE2API

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-blue?logo=python" alt="Python Version" />
  <img src="https://img.shields.io/badge/FastAPI-0.115%2B-009688?logo=fastapi" alt="FastAPI" />
  <img src="https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker" alt="Docker" />
  <img src="https://img.shields.io/badge/API-OpenAI%20Compatible-green" alt="OpenAI API Compatible" />
  <img src="https://img.shields.io/badge/License-MIT-orange" alt="License" />
</p>

将 **[muse.ai](https://muse.ai/)** 网页端的前沿多模态能力逆向工程封装为标准的 **OpenAI 兼容 RESTful API**。通过无头浏览器 CDP 协议穿透与动态 Session 管理，原生支持文本对话、文生图、文生视频、首帧图生视频，并提供多账号池轮转、全自动 48 小时会话保活以及配套 Chrome 导号扩展。

---

## 🌟 核心特性

- 💬 **标准对话接口（Chat Completions）**
  - 完全兼容 `/v1/chat/completions` 标准协议。
  - 原生支持 SSE 流式打字机输出（`stream=True`）与同步完整返回。
  - 支持多轮对话上下文、System Prompt 设定。
  - 内置模型智能别名映射，`gpt-4o`、`claude-3-5-sonnet`、`deepseek-chat` 等常用模型名自动路由。
- 🎨 **高质量生图（Images Generations）**
  - 完全兼容 `/v1/images/generations` 接口。
  - 支持 `1:1`、`16:9`、`9:16`、`4:3` 等多画幅生成。
  - 支持 `url` 直链或 `b64_json` 两种返回格式。
- 🎬 **文生视频 / 图生视频（Videos）**
  - 原生对接 Muse 顶配视频生成模型，支持 5 秒 / 10 秒多画幅视频生成。
  - 异步任务架构（`/v1/videos` 创建任务 + `/v1/videos/{task_id}` 状态轮询）。
  - 内置媒体资源服务 `/v1/media/{filename}`，自动持久化存储生成的 MP4 / WebP 资源。
- 🔄 **多账号池与负载均衡**
  - 支持导入无上限的 Muse 账号矩阵。
  - 基于 LRU（最久未使用）算法自动分发任务，均衡消耗额度。
  - 遇到单号额度耗尽或异常时，自动标记并隔离，无感故障转移。
- 🛡️ **48 小时会话全自动保活续期**
  - 独创后台心跳协程，定时静默校验会话活性并自动刷新 Meta Session 令牌。
  - 解决 Meta Cookie 静态 48 小时到期难题，无需频繁重新登录。
- 🧩 **配套 Chrome 一键导号扩展**
  - 无需手工 F12 抓包，点一下扩展图标即刻将当前浏览器登录态提取并安全推送至账号池。
- 🖥️ **现代化深色运维面板（Web Console）**
  - 内置开箱即用的 Web UI，支持实时查看服务健康度、账号池额度与状态、任务进度回放、媒体库管理与在线接口调试。

---

## 🚀 极速部署

### 方式一：Docker Compose（推荐，一行命令开箱即用）

1. **克隆代码并进入目录**：
   ```bash
   git clone https://github.com/your-username/muse2api.git
   cd muse2api
   ```

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

## 📄 开源许可证

本项目基于 [MIT License](LICENSE) 开源发布。
