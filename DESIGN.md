# Gemini API Key 号池设计方案

## 概述

一个基于 FastAPI 的 Gemini API 代理服务，管理多个来自不同 Google Cloud 项目的 API Key，通过代理模式精确追踪每个 Key 的用量，自动选择剩余 RPD 最多的 Key 转发请求，遇到 429 错误自动切换 Key 重试。

## 架构

```
调用方                    号池代理 (FastAPI)              Gemini API
  │                           │                            │
  │  POST /v1beta/models/...  │                            │
  │ ─────────────────────────→│                            │
  │                           │  选择剩余 RPD 最大的 Key    │
  │                           │  注入 x-goog-api-key 头    │
  │                           │  POST generativelanguage   │
  │                           │───────────────────────────→│
  │                           │                            │
  │                           │  200 OK / 429 Error        │
  │                           │←───────────────────────────│
  │                           │                            │
  │                           │  如果 429: 换 Key 重试      │
  │                           │───────────────────────────→│
  │                           │                            │
  │  返回 Gemini 响应          │                            │
  │←──────────────────────────│                            │
```

### 核心设计决策

| 决策 | 选择 | 原因 |
|------|------|------|
| 架构模式 | 代理转发 | 号池能精确追踪每次请求，无需调用方配合 |
| Key 来源 | 不同 Google Cloud 项目 | 每个 Key 有独立的 RPD 配额 |
| Key 选择策略 | 剩余 RPD 最大优先 | 最大化可用性，避免单 Key 过载 |
| 429 处理 | 自动换 Key 重试 | 对调用方透明，无需感知 Key 管理 |
| 用量追踪 | 内存计数 + 429 反馈 | 代理模式下每次请求都经过号池，计数精确 |
| 状态持久化 | 内存（重启归零） | 简单场景足够，RPD 每天重置 |
| 技术栈 | Python + FastAPI + httpx | 异步高性能，生态成熟 |

## API 设计

### 1. 代理端点（核心）

所有 Gemini API 请求直接发到号池，号池透明转发。

```
任意路径: /{path:path}
方法: 任意 (GET, POST, PUT, DELETE)
```

调用方只需把 Gemini API 的 base URL 从 `https://generativelanguage.googleapis.com` 改为 `http://localhost:8000`，其他不变。

**示例：**

```bash
# 原本直接调 Gemini:
curl -X POST "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent" \
  -H "x-goog-api-key: YOUR_KEY" \
  -d '{"contents":[{"parts":[{"text":"Hello"}]}]}'

# 改为调号池代理（不需要传 Key）:
curl -X POST "http://localhost:8000/v1beta/models/gemini-2.5-flash:generateContent" \
  -d '{"contents":[{"parts":[{"text":"Hello"}]}]}'
```

**文件上传（URI 分析）同理：**

```bash
# 上传文件
curl -X POST "http://localhost:8000/upload/v1beta/files" \
  -H "Content-Type: application/pdf" \
  --data-binary @document.pdf

# 用返回的 URI 分析文件
curl -X POST "http://localhost:8000/v1beta/models/gemini-2.5-flash:generateContent" \
  -d '{
    "contents": [{
      "parts": [
        {"file_data": {"file_uri": "https://generativelanguage.googleapis.com/v1beta/files/xxx", "mime_type": "application/pdf"}},
        {"text": "分析这个文件"}
      ]
    }]
  }'
```

### 2. 管理端点

```
GET  /admin/status          # 查看所有 Key 的状态和剩余 RPD
GET  /admin/status/{key_id} # 查看单个 Key 的详细状态
POST /admin/reset           # 手动重置所有计数器
POST /admin/keys            # 动态添加 Key
DELETE /admin/keys/{key_id} # 动态移除 Key
```

**状态响应示例：**

```json
{
  "total_keys": 5,
  "available_keys": 4,
  "exhausted_keys": 1,
  "next_reset": "2026-02-14T08:00:00Z",
  "keys": [
    {
      "id": "key_1",
      "key_prefix": "AIzaSy...abc",
      "rpd_limit": 250,
      "rpd_used": 42,
      "rpd_remaining": 208,
      "rpm_current": 3,
      "last_used": "2026-02-13T13:25:00Z",
      "last_error": null,
      "status": "active"
    },
    {
      "id": "key_2",
      "key_prefix": "AIzaSy...def",
      "rpd_limit": 250,
      "rpd_used": 250,
      "rpd_remaining": 0,
      "rpm_current": 0,
      "last_used": "2026-02-13T12:00:00Z",
      "last_error": "2026-02-13T12:00:00Z",
      "status": "exhausted"
    }
  ]
}
```

## 数据模型

```python
@dataclass
class ApiKey:
    id: str                    # 内部标识 (key_1, key_2, ...)
    key: str                   # 完整 API Key
    project_id: str            # Google Cloud 项目标识（可选，用于分组）
    rpd_limit: int             # 每日请求上限 (默认 250)
    rpm_limit: int             # 每分钟请求上限 (默认 10)
    rpd_used: int              # 今日已用请求数
    rpm_timestamps: list[float]  # 最近 60 秒的请求时间戳
    last_used: datetime | None
    last_error: datetime | None
    consecutive_failures: int  # 连续失败次数
    status: str                # active | exhausted | disabled | cooldown

@dataclass
class PoolState:
    keys: dict[str, ApiKey]
    last_reset_date: date      # 上次重置日期（太平洋时间）
```

## 核心逻辑

### Key 选择算法

```
1. 过滤可用 Key: status == "active" 且 rpd_remaining > 0 且 rpm_current < rpm_limit
2. 按 rpd_remaining 降序排序
3. 返回第一个（剩余 RPD 最多的 Key）
4. 如果没有可用 Key，返回 503 Service Unavailable
```

### 429 自动重试

```
1. 用选中的 Key 转发请求到 Gemini
2. 如果返回 429:
   a. 解析错误详情，判断是 RPM 还是 RPD 限制
   b. 如果是 RPD: 标记该 Key 为 exhausted，选下一个 Key 重试
   c. 如果是 RPM: 等待短暂时间后用同一个 Key 重试
   d. 最多重试 3 次（可配置）
3. 如果所有 Key 都耗尽，返回 503 给调用方
```

### RPD 每日重置

RPD 在太平洋时间午夜（UTC 08:00）重置。

```
方案: 惰性重置
- 每次请求时检查当前太平洋时间日期
- 如果日期 > last_reset_date，重置所有 Key 的计数器
- 无需后台定时任务，简单可靠
```

注意：太平洋时间有夏令时（PDT = UTC-7）和冬令时（PST = UTC-8），需要用 `pytz` 或 `zoneinfo` 正确处理。

### RPM 滑动窗口

```
- 维护每个 Key 最近 60 秒的请求时间戳列表
- 每次请求前清理超过 60 秒的时间戳
- 当前 RPM = len(timestamps)
- 如果 RPM >= rpm_limit，该 Key 暂时不可用
```

## 配置

通过环境变量或 `.env` 文件配置：

```env
# API Keys（逗号分隔）
GEMINI_API_KEYS=AIzaSy...key1,AIzaSy...key2,AIzaSy...key3

# 服务配置
PORT=8000
HOST=0.0.0.0

# 限流配置（Free Tier 默认值）
DEFAULT_RPD_LIMIT=250
DEFAULT_RPM_LIMIT=10

# 重试配置
MAX_RETRIES=3
RETRY_DELAY_SECONDS=2

# Gemini API 目标地址
GEMINI_BASE_URL=https://generativelanguage.googleapis.com

# 日志级别
LOG_LEVEL=INFO
```

## 项目结构

```
gemini-proxy/
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI 入口，路由注册
│   ├── config.py            # 配置加载
│   ├── models.py            # 数据模型
│   ├── key_manager.py       # Key 池管理（选择、追踪、重置）
│   ├── proxy.py             # 代理转发逻辑（httpx 转发 + 429 重试）
│   └── admin.py             # 管理端点
├── .env.example             # 配置模板
├── requirements.txt         # 依赖
├── Dockerfile               # 容器化（可选）
└── DESIGN.md                # 本文档
```

## 依赖

```
fastapi>=0.115.0
uvicorn>=0.34.0
httpx>=0.28.0
python-dotenv>=1.0.0
```

## 调用方集成

调用方只需修改 Gemini API 的 base URL：

### Python (google-generativeai SDK)

```python
import google.generativeai as genai

# 方式 1: 设置环境变量
# GEMINI_API_BASE_URL=http://localhost:8000

# 方式 2: 直接用 httpx 调用代理
import httpx

response = httpx.post(
    "http://localhost:8000/v1beta/models/gemini-2.5-flash:generateContent",
    json={"contents": [{"parts": [{"text": "Hello"}]}]}
)
```

### curl

```bash
# 不需要传 API Key，代理自动注入
curl -X POST "http://localhost:8000/v1beta/models/gemini-2.5-flash:generateContent" \
  -H "Content-Type: application/json" \
  -d '{"contents":[{"parts":[{"text":"Hello"}]}]}'
```

## 限制与注意事项

1. **内存状态**: 服务重启后计数器归零。如果需要持久化，可以后续加 SQLite/Redis。
2. **单实例**: 当前设计为单实例部署。多实例需要共享状态存储。
3. **文件上传**: 大文件上传会经过代理，增加内存占用。httpx 支持流式转发可缓解。
4. **无认证**: 仅限 localhost 使用，不暴露到公网。如需认证可加 Bearer Token 中间件。
5. **夏令时**: 太平洋时间有 PST/PDT 切换，重置时间在 UTC 07:00 或 08:00 之间变化。

## Gemini API 速率限制参考

| 层级 | 模型 | RPM | TPM | RPD |
|------|------|-----|-----|-----|
| Free | Gemini 2.5 Flash | 10 | 250K | 250 |
| Free | Gemini 2.5 Pro | 5 | 250K | 100 |
| Free | Gemini 2.5 Flash-Lite | 15 | 250K | 1,000 |
| Tier 1 | Gemini 2.5 Flash | 300 | 2M | 1,500 |
| Tier 1 | Gemini 2.5 Pro | 150 | 1M | 1,000 |

RPD 在太平洋时间午夜重置。限制是项目级别的，不同项目的 Key 有独立配额。
