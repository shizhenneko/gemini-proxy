# Gemini API Key 号池代理

基于 FastAPI 的 Gemini API 代理服务，管理多个来自不同 Google Cloud 项目的 API Key，自动选择剩余配额最多的 Key 转发请求，遇到 429 错误自动切换 Key 重试。

## 功能

- 透明代理：调用方只需修改 base URL，无需管理 Key
- Key 池管理：支持多个 Key，自动选择剩余 RPD 最多的 Key
- RPD/RPM 追踪：精确追踪每个 Key 的每日和每分钟请求数
- 429 自动重试：遇到速率限制自动切换 Key 重试（区分 RPD/RPM 限制）
- SSE 流式响应：支持 `alt=sse` 流式输出
- SDK 直连支持：为无法使用 HTTP 代理的 SDK 客户端（如 `google-genai`）提供 Key 分配 API
- 管理端点：查看 Key 状态、手动重置计数器、动态添加/移除 Key

## 环境要求

- Python 3.8+

## 安装

```bash
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env 文件，设置 GEMINI_API_KEYS
```

## 运行

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Docker

```bash
docker build -t gemini-proxy .
docker run -p 8000:8000 --env-file .env gemini-proxy
```

## 使用方法

### 代理请求

将 Gemini API 的 base URL 从 `https://generativelanguage.googleapis.com` 改为 `http://localhost:8000`：

```bash
# 生成内容
curl -X POST "http://localhost:8000/v1beta/models/gemini-2.5-flash:generateContent" \
  -H "Content-Type: application/json" \
  -d '{"contents":[{"parts":[{"text":"Hello"}]}]}'

# 上传文件
curl -X POST "http://localhost:8000/upload/v1beta/files" \
  -H "Content-Type: application/pdf" \
  --data-binary @document.pdf
```

### SDK 直连模式

对于无法设置自定义 base URL 的 SDK 客户端（如 `google-genai`），可通过号池分配真实 API Key 后直连 Google API：

```python
import requests
from google import genai

PROXY = "http://localhost:8000"

# 1. 从号池分配一个可用 Key
resp = requests.post(f"{PROXY}/sdk/allocate-key")
data = resp.json()  # {"key_id": "key_1", "api_key": "AIzaSy..."}

# 2. 用真实 Key 初始化 SDK，直连 Google API
client = genai.Client(api_key=data["api_key"])

# 3. 正常使用 SDK（如上传视频、生成内容等）
video_file = client.files.upload(path="video.mp4")
response = client.models.generate_content(
    model="gemini-2.0-flash-exp",
    contents=[{"role": "user", "parts": [
        {"file_data": {"file_uri": video_file.uri}},
        {"text": "分析这个视频"},
    ]}],
)

# 4. 调用完成后向号池报告用量
requests.post(f"{PROXY}/sdk/report-usage", json={"key_id": data["key_id"]})

# 如果调用失败，报告错误
# requests.post(f"{PROXY}/sdk/report-error", json={"key_id": data["key_id"], "is_rpd_limit": False})
```

### 管理端点

```bash
# 查看所有 Key 状态
curl http://localhost:8000/admin/status

# 查看单个 Key 状态
curl http://localhost:8000/admin/status/key_1

# 手动重置计数器
curl -X POST http://localhost:8000/admin/reset

# 添加 Key（参数：api_key 必填，rpd_limit/rpm_limit 可选）
curl -X POST http://localhost:8000/admin/keys \
  -H "Content-Type: application/json" \
  -d '{"api_key":"AIzaSy...","rpd_limit":250,"rpm_limit":10}'

# 移除 Key
curl -X DELETE http://localhost:8000/admin/keys/key_1
```

## API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 服务状态（可用/总计 Key 数） |
| `/health` | GET | 健康检查 |
| `/{path:path}` | 任意 | 透明代理所有 Gemini API 请求 |
| `/sdk/allocate-key` | POST | 从号池分配一个可用 API Key |
| `/sdk/report-usage` | POST | 报告 SDK 直连的成功调用 |
| `/sdk/report-error` | POST | 报告 SDK 直连的调用错误 |
| `/admin/status` | GET | 查看所有 Key 状态 |
| `/admin/status/{key_id}` | GET | 查看单个 Key 状态 |
| `/admin/reset` | POST | 手动重置所有计数器 |
| `/admin/keys` | POST | 动态添加 Key |
| `/admin/keys/{key_id}` | DELETE | 动态移除 Key |

## 配置说明

| 环境变量 | 说明 | 默认值 |
|---------|------|--------|
| `GEMINI_API_KEYS` | 逗号分隔的 API Key 列表（必填） | - |
| `PORT` | 服务端口 | 8000 |
| `HOST` | 监听地址 | 0.0.0.0 |
| `DEFAULT_RPD_LIMIT` | 每日请求上限（Free Tier） | 250 |
| `DEFAULT_RPM_LIMIT` | 每分钟请求上限（Free Tier） | 10 |
| `MAX_RETRIES` | 429 错误最大重试次数 | 3 |
| `RETRY_DELAY_SECONDS` | 重试延迟（秒） | 2 |
| `GEMINI_BASE_URL` | Gemini API 目标地址 | https://generativelanguage.googleapis.com |
| `LOG_LEVEL` | 日志级别 | INFO |

## 限制

- 单实例部署：多实例需要共享状态存储
- 内存状态：服务重启后计数器归零
- 无认证：仅限 localhost 使用，不暴露到公网
- 太平洋时区：RPD 在太平洋时间午夜（UTC 08:00）重置

## 测试

```bash
pytest tests/ -v
```
