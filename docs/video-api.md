# 视频接口对接

线上地址：`https://muse.secure-skill.com`

视频生成是异步的。先创建任务，拿到 `id`，再轮询这个 `id`，直到 `status` 变成 `completed` 或 `failed`。创建成功只表示任务已进队列。

## 鉴权

除健康检查外，请求都要带管理后台的 API Key：

```http
Authorization: Bearer m2a_你的密钥
Content-Type: application/json
```

密钥以 `m2a_` 开头，和打开 `https://muse.secure-skill.com/admin` 时填写的是同一把。不要把密钥写进前端页面或公开仓库。

## 接口

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/v1/videos` | 创建视频任务 |
| POST | `/v1/videos/generations` | 与上面相同 |
| GET | `/v1/videos/{task_id}` | 查询任务 |
| GET | `/v1/videos/generations/{task_id}` | 与上面相同 |

创建接口返回 HTTP 200。查询接口在任务不存在时返回 HTTP 404。

## 创建任务

```bash
curl -X POST "https://muse.secure-skill.com/v1/videos" \
  -H "Authorization: Bearer m2a_你的密钥" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "白纸上一个红色方块缓慢旋转一圈，画面干净",
    "duration": 6,
    "aspect_ratio": "16:9"
  }'
```

成功时：

```json
{
  "id": "task_33ec6bd284dc459bbe94",
  "task_id": "task_33ec6bd284dc459bbe94",
  "object": "video.task",
  "status": "queued",
  "progress": 0,
  "queue_position": 1,
  "created_at": 1791555865
}
```

`id` 和 `task_id` 相同。之后只用这个值查询。重复 POST 会新建一条任务，不会复用上一次的结果。

### 请求字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `prompt` | 是 | 画面描述 |
| `duration` | 否 | 希望的秒数，正整数。不传时按 6 秒请求。服务端不截断这个数字，成片实际时长由上游决定。线上已成功生成过 6 秒和 30 秒 |
| `aspect_ratio` | 否 | 画幅。见下文 |
| `size` | 否 | 和 `aspect_ratio` 一起识别画幅，写其中一个即可 |
| `model` | 否 | 默认 `muse-video`。这个路径固定生成视频，模型名只作兼容 |
| `resolution` | 否 | 附加画质说明，例如 `720p` |
| `extra` | 否 | 附加在提示词后面的补充要求 |
| `timeout` | 否 | 这一次生成最多等待的秒数，默认 600。这是任务时限，不是视频时长 |
| `reference_images` | 否 | 参考图数组，最多 10 张。见下文 |
| `images` | 否 | 参考图的另一种字段名 |
| `image_urls` | 否 | 参考图 URL 数组 |
| `reference_image` | 否 | 单张参考图 |
| `image_url` | 否 | 单张参考图 |
| `image` | 否 | 单张参考图 |

### 画幅

`aspect_ratio` 和 `size` 不区分大小写，命中下面任一写法就会按对应画幅请求：

| 画幅 | 可写的值 |
|---|---|
| 16:9 横屏 | `16:9`、`16/9`、`landscape`、`横屏`、`1280x720`、`1920x1080` |
| 9:16 竖屏 | `9:16`、`9/16`、`portrait`、`竖屏`、`720x1280`、`1080x1920` |

两个字段都传时，只要有一个命中就会生效。都不命中时，不额外指定画幅。

## 查询任务

```bash
curl "https://muse.secure-skill.com/v1/videos/task_33ec6bd284dc459bbe94" \
  -H "Authorization: Bearer m2a_你的密钥"
```

建议每 3 到 5 秒查一次。6 秒视频通常要几十秒到两分钟。请一直查到终态，查询本身不会重新生成。

| `status` | 含义 |
|---|---|
| `queued` | 在排队，还没有账号开始做 |
| `processing` | 正在生成。`progress` 是估算值，完成前最高到 92 |
| `completed` | 成功。`progress` 为 100，读取 `url` |
| `failed` | 失败。读取 `error` |

完成时关注这些字段：

```json
{
  "id": "task_33ec6bd284dc459bbe94",
  "status": "completed",
  "progress": 100,
  "url": "https://source.openclaw-token.shop/uploads/1791555939-c337b6dc2ee2.mp4",
  "video": {
    "url": "https://source.openclaw-token.shop/uploads/1791555939-c337b6dc2ee2.mp4"
  },
  "result": {
    "url": "https://source.openclaw-token.shop/uploads/1791555939-c337b6dc2ee2.mp4",
    "filename": "1791555939-c337b6dc2ee2.mp4",
    "kind": "video",
    "bytes": 1870525
  },
  "elapsed": 75.5,
  "error": null
}
```

成品地址用 `url`。`video.url` 和 `result.url` 是同一个地址。这是图床上的公网地址，下载时不需要 API Key。用 GET 下载。响应里可能还有内部字段，对接时忽略即可。

失败时：

```json
{
  "id": "task_xxx",
  "status": "failed",
  "error": "没有空闲账号，等待超时"
}
```

## 参考图

不传参考图就是文生视频。传入后按数组顺序作为参考图，最多 10 张。同一张图重复出现只保留第一次。超过 10 张时，创建接口直接返回 HTTP 400，不会生成。

下面几种写法可以混用，收集顺序是 `reference_images`、`images`、`image_urls`、`reference_image`、`image_url`、`image`。

公网图片 URL：

```json
{
  "prompt": "让画面里的角色向前走",
  "duration": 6,
  "aspect_ratio": "16:9",
  "reference_images": [
    "https://example.com/a.png",
    "https://example.com/b.png"
  ]
}
```

一张图也可以写成字符串：

```json
{ "prompt": "从这张图开始动起来", "image_url": "https://example.com/a.png" }
```

OpenAI 风格对象，或纯 base64：

```json
{
  "prompt": "从这张图开始动起来",
  "images": [
    { "image_url": { "url": "https://example.com/a.png" } },
    { "b64_json": "<纯 base64，不要带 data: 前缀>" }
  ]
}
```

也支持完整的 data URL：`data:image/png;base64,...`。参考图需要服务端能下载，或直接放在请求体里。

## 排队

一个账号同时做一条视频。有多个账号时，多条任务会同时做。超出账号数量的任务留在队列里，`status` 保持 `queued`，`queue_position` 从 1 开始。

客户端可以连续提交，用各自的 `id` 轮询。队列长度在管理后台设置，默认 20，范围 1 到 500。队列满了再提交会返回 HTTP 429。

## 错误

`/v1/` 的错误体是：

```json
{
  "error": {
    "message": "参考图最多 10 张",
    "type": "invalid_request_error",
    "param": null,
    "code": 400
  }
}
```

| HTTP | 场景 | `message` |
|---|---|---|
| 401 | 没带密钥或密钥错误 | `缺少 Authorization: Bearer <key>` 或 `API key 无效` |
| 400 | 参考图超过 10 张 | `参考图最多 10 张` |
| 400 | 没有可生成的账号 | `没有可用账号，请先在管理页面导入 cookie` |
| 422 | 缺少 `prompt` 或字段类型不对 | `请求参数校验失败：...` |
| 429 | 队列已满 | `队列已满，请稍后再试` |
| 503 | 队列服务不可用 | `任务队列不可用，请确认 Redis 已启动` |
| 404 | 任务 id 不存在 | `task 不存在` |

生成过程中的失败不会体现在创建请求上。创建已经返回 200 之后，失败会写在查询结果的 `status=failed` 和 `error` 里。常见原因包括等待空闲账号超时、上游只回了文字没有视频、成品超过图床 100MB。

## Python

```python
import time
import requests

BASE = "https://muse.secure-skill.com"
HEADERS = {"Authorization": "Bearer m2a_你的密钥"}

created = requests.post(
    f"{BASE}/v1/videos",
    headers=HEADERS,
    json={
        "prompt": "白纸上一个红色方块缓慢旋转一圈，画面干净",
        "duration": 6,
        "aspect_ratio": "16:9",
    },
    timeout=30,
)
created.raise_for_status()
task_id = created.json()["id"]

deadline = time.time() + 600
while True:
    task = requests.get(f"{BASE}/v1/videos/{task_id}", headers=HEADERS, timeout=30)
    task.raise_for_status()
    body = task.json()
    if body["status"] == "completed":
        print(body["url"])
        break
    if body["status"] == "failed":
        raise RuntimeError(body.get("error") or "视频生成失败")
    if time.time() > deadline:
        raise TimeoutError(task_id)
    time.sleep(5)
```

终态是 `completed`。不要等 `succeeded`。

## JavaScript

```javascript
const BASE = "https://muse.secure-skill.com";
const headers = {
  Authorization: "Bearer m2a_你的密钥",
  "Content-Type": "application/json",
};

const created = await fetch(`${BASE}/v1/videos`, {
  method: "POST",
  headers,
  body: JSON.stringify({
    prompt: "白纸上一个蓝色圆球缓慢弹起落下，画面干净",
    duration: 6,
    aspect_ratio: "16:9",
  }),
});
if (!created.ok) throw new Error(JSON.stringify(await created.json()));
const { id } = await created.json();

const deadline = Date.now() + 600_000;
while (true) {
  const res = await fetch(`${BASE}/v1/videos/${id}`, { headers });
  if (!res.ok) throw new Error(JSON.stringify(await res.json()));
  const task = await res.json();
  if (task.status === "completed") {
    console.log(task.url);
    break;
  }
  if (task.status === "failed") throw new Error(task.error || "视频生成失败");
  if (Date.now() > deadline) throw new Error("timeout");
  await new Promise((resolve) => setTimeout(resolve, 5000));
}
```
