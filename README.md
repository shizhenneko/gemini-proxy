# Gemini API Key Pool Proxy (Gemini 密钥池代理)

这是一个基于 FastAPI 构建的高可用 Gemini API 代理服务。它能够管理多个 Google Cloud 项目的 API Key，智能地分发请求以最大化利用配额，并自动处理速率限制（Rate Limits）。

## 核心功能

- **智能负载均衡**：自动选择剩余每日配额（RPD）最多的 API Key 来转发请求，确保负载均匀分布。
- **智能速率限制处理**：
  - **区分 RPD 与 RPM**：精确区分每日请求限制（RPD）和每分钟请求限制（RPM）。
  - **自动重试**：遇到 429 错误说明 RPD 耗尽时，自动切换到下一个可用 Key 重试。
  - **RPM 避让**：遇到 RPM 限制时，暂时挂起该 Key，同时使用其他 Key 继续服务。
- **SDK 直连支持**：专为无法修改 Base URL 的客户端（如官方 `google-genai` SDK）设计，提供 Key 分配接口，允许客户端“借用”真实 Key 直连 Google 服务器。
- **SSE 流式支持**：完美支持 `alt=sse` 的流式响应。
- **管理与监控**：内置管理端点，可查看 Key 状态、手动重置配额、动态添加/删除 Key。

## 环境要求

- Python 3.8+
- Docker (可选)

## 安装部署

1. **克隆仓库：**
   ```bash
   git clone https://github.com/yourusername/gemini-proxy.git
   cd gemini-proxy
   ```

2. **安装依赖：**
   ```bash
   pip install -r requirements.txt
   ```

3. **配置环境：**
   复制示例配置文件并填入你的 API Key。
   ```bash
   cp .env.example .env
   ```
   
   编辑 `.env` 文件：
   ```ini
   # 多个 Key 用逗号分隔
   GEMINI_API_KEYS=key1,key2,key3,...
   ```

## 配置说明

| 变量名                | 说明                                    | 默认值    |
| --------------------- | --------------------------------------- | --------- |
| `GEMINI_API_KEYS`     | Gemini API Key 列表，逗号分隔 (必填)。  | -         |
| `PORT`                | 服务监听端口。                          | `8000`    |
| `HOST`                | 服务监听地址。                          | `0.0.0.0` |
| `DEFAULT_RPD_LIMIT`   | 每个 Key 的每日请求上限 (Free Tier)。   | `250`     |
| `DEFAULT_RPM_LIMIT`   | 每个 Key 的每分钟请求上限 (Free Tier)。 | `10`      |
| `MAX_RETRIES`         | 遇到 429 错误时的最大重试次数。         | `3`       |
| `RETRY_DELAY_SECONDS` | 遇到 RPM 限制时的重试延迟(秒)。         | `2`       |
| `LOG_LEVEL`           | 日志级别 (DEBUG, INFO, WARNING, ERROR). | `INFO`    |

## 使用指南

### 1. 启动服务

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```
或使用 Docker：
```bash
docker run -p 8000:8000 --env-file .env gemini-proxy
```

### 2. 标准代理模式 (Proxy Mode)

适用于支持自定义 Base URL 的客户端（如 OpenAI 兼容客户端或即使 HTTP 请求）。
只需将 Gemini API 的 Base URL 从 `https://generativelanguage.googleapis.com` 替换为你的代理地址（例如 `http://localhost:8000`）。

**cURL 示例：**
```bash
curl -X POST "http://localhost:8000/v1beta/models/gemini-1.5-flash:generateContent" \
  -H "Content-Type: application/json" \
  -d '{
    "contents": [{
      "parts": [{"text": "用一句话解释量子计算。"}]
    }]
  }'
```

**关于 429 错误处理：**
在代理模式下，**客户端无需关心 429 错误**。代理服务会自动捕获 Google 返回的 429 响应：
- 如果是 **RPD (每日配额) 耗尽**：代理会自动标记该 Key 为耗尽，并立即使用下一个可用 Key 重试请求。
- 如果是 **RPM (每分钟请求) 限制**：代理会等待几秒钟（`RETRY_DELAY_SECONDS`），然后重试或切换 Key。

### 3. SDK 直连模式 (SDK Direct Mode)

适用于 **无法修改 Base URL** 的官方 SDK（例如 `google-genai` Python SDK），或者你需要直接与 Google 服务器建立连接以获得最低延迟的场景。

在此模式下，客户端需要从号池“借用”一个 Key，用完还需要“归还”状态（报错机制）。

**工作流程：**

1. **申请 Key (`/sdk/allocate-key`)**：从号池获取一个当前可用的 Key。
2. **初始化 SDK**：用申请到的 Key 初始化官方 SDK。
3. **执行操作**：直接调用 Google API。
4. **上报结果**：
   - 成功：调用 `/sdk/report-usage` 记录使用次数。
   - 失败 (429)：调用 `/sdk/report-error` 报告 Key 失效。

**Python 示例代码：**

```python
import requests
from google import genai
from google.genai import types

PROXY_URL = "http://localhost:8000"

def get_gemini_client():
    # 1. 从号池申请一个可用 Key
    try:
        response = requests.post(f"{PROXY_URL}/sdk/allocate-key")
        response.raise_for_status()
        data = response.json()
        return data["api_key"], data["key_id"]
    except requests.exceptions.RequestException:
        print("号池暂时无可用 Key！")
        return None, None

def report_success(key_id):
    # 4. 上报成功使用
    requests.post(f"{PROXY_URL}/sdk/report-usage", json={"key_id": key_id})

def report_failure(key_id, error):
    # 5. 上报错误 (关键步骤)
    # 客户端需要判断错误类型，如果是 429 Resource Exhausted，告知号池
    is_429 = "429" in str(error) or "ResourceExhausted" in str(error)
    requests.post(f"{PROXY_URL}/sdk/report-error", json={
        "key_id": key_id,
        "is_rpd_limit": is_429  # 标记是否为配额耗尽
    })

# 使用流程
api_key, key_id = get_gemini_client()
if api_key:
    client = genai.Client(api_key=api_key)
    try:
        # 2/3. 直连 Google API
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents="你好，Gemini！"
        )
        print(response.text)
        report_success(key_id)
        
    except Exception as e:
        print(f"调用出错: {e}")
        report_failure(key_id, e)
        # 此时可以重新调用 get_gemini_client() 获取新 Key 重试
```

**SDK 模式下的 429 处理机制：**
- 当你在客户端收到 429 错误时，**必须** 调用 `/sdk/report-error` 并设置 `is_rpd_limit=True`。
- 号池收到报告后，会将该 Key 标记为 `EXHAUSTED`（耗尽），并在当天不再分配该 Key。
- 你随后再次调用 `/sdk/allocate-key` 时，号池会自动给你分配另一个健康的 Key。

### 4. 管理接口

- **查看状态**: `GET /admin/status`
- **重置配额**: `POST /admin/reset` (强制重置所有 Key 的计数器)
- **添加 Key**: `POST /admin/keys` (Body: `{"api_key": "...", "rpd_limit": 1000}`)
- **移除 Key**: `DELETE /admin/keys/{key_id}`

## 测试

使用 pytest 运行测试套件：

```bash
pytest
```
