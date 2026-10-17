# CC Switch 代理桥

基于 Django 6 + [uv](https://docs.astral.sh/uv/) 的 Claude Code 代理桥。它在多个账号与第三方供应商之间路由请求，并**保证每个账号额度的最后 5% 不被消耗**：账号到达保留线后自动切换到下一个账号；官方账号全部到线后切换到第三方供应商；全部不可用才返回 `429`。

## 功能

- 透明代理 Anthropic `POST /v1/messages`，支持 SSE 流式转发与用量解析
- 官方订阅账号：读取 `anthropic-ratelimit-unified-5h/7d-utilization` 响应头
- Anthropic API Key：按本地预算累计花费，月度自动重置
- 第三方供应商：可配置 HTTP 余额查询（DeepSeek/OpenRouter/自定义 JSON 路径），带 TTL 缓存
- 官方账号优先、第三方兜底的层级路由；`401/403/429/5xx` 自动降级到下一个账号
- 并发上限、失败冷却、请求日志、Django Admin、状态接口

## 快速开始

```bash
cp .env.example .env          # 至少修改 BRIDGE_TOKEN 与 DJANGO_SECRET_KEY
uv sync
uv run python manage.py migrate
uv run python manage.py createsuperuser
uv run uvicorn cc_switch.asgi:application --host 127.0.0.1 --port 8000
```

> 流式转发依赖 ASGI，请使用 `uvicorn`（`manage.py runserver` 为 WSGI，长连接 SSE 不可靠）。

打开 <http://127.0.0.1:8000/admin/> 添加上游账号。

## 账号配置

| 场景 | tier | auth_type | quota_mode | 说明 |
| --- | --- | --- | --- | --- |
| Claude Code 订阅 | `official` | `oauth` | `headers` | 填 access token，额度取自响应头 |
| Anthropic API Key | `official` | `api_key` | `budget` | 填预算 `budget_usd`，按 token 成本累计 |
| 第三方供应商 | `third_party` | `api_key` | `probe` | 填 `base_url` 与余额查询配置 |

凭据在保存时用 Fernet 加密（密钥来自 `BRIDGE_ENCRYPTION_KEY`，未配置时回退到 `DJANGO_SECRET_KEY`）。

## 第三方供应商额度查询（本次修复）

CC Switch 旧的 usage script 只读取 `response.balance`：

```js
extractor: function(response) {
  return { isValid: response.is_active || true, remaining: response.balance, unit: "USD" };
}
```

而 DeepSeek 兼容中转返回的是：

```json
{"is_available": true, "balance_infos": [{"currency": "USD", "total_balance": "7.5"}]}
```

`response.balance` 为 `undefined`，余额因此始终查不到。本项目的 `quota_probe` 同时兼容两种结构：

```json
{
  "url": "{base_url}/user/balance",
  "method": "GET",
  "headers": {"Authorization": "Bearer {api_key}"},
  "remaining_paths": ["balance", "balance_infos[*].total_balance"],
  "total_path": null,
  "active_path": "is_available"
}
```

- `remaining_paths` 按顺序尝试，支持 `a.b.c` 与 `a[*].b`（数组求和）
- 利用率 `= 1 - remaining / total`；`total` 未提供时使用账号的 `budget_usd` 作为分母
- `active_path` 为 `false` 时直接判定额度耗尽
- 结果缓存 `BRIDGE_PROBE_TTL_SECONDS`（默认 300 秒），也可手动刷新：

```bash
uv run python manage.py refresh_quotas --force
curl -H "x-bridge-token: $BRIDGE_TOKEN" \
     http://127.0.0.1:8000/api/accounts/<account_id>/refresh -X POST
```

## 保留线规则

```
limit = 1 - reserve_ratio - safety_margin   # 默认 1 - 0.05 - 0.005 = 94.5%
```

- 任一窗口（5h / 7d / 预算 / 余额）利用率 ≥ `limit`，账号即被跳过
- 官方账号全部到线 → 使用第三方供应商；第三方也到线 → `429 rate_limit_error`
- `inflight` 达到 `max_inflight` 的账号暂时不再接新请求，减少并发越线
- `probe` 模式必须先成功查到一次余额（`BRIDGE_PROBE_REQUIRED=1`），否则不会路由，避免“额度未知却继续花”

> 该策略按上游响应头判定并预留 0.5% 安全余量；极端并发下仍可能因响应滞后出现轻微越线。

## 客户端接入

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8000
export ANTHROPIC_AUTH_TOKEN=<BRIDGE_TOKEN>
```

桥会忽略客户端带来的凭据并注入对应账号的凭据；`x-bridge-token`、`Authorization: Bearer`、`x-api-key` 任一方式携带 `BRIDGE_TOKEN` 都可通过桥的鉴权。

## 状态接口

```bash
curl -H "x-bridge-token: $BRIDGE_TOKEN" http://127.0.0.1:8000/api/status
curl -H "x-bridge-token: $BRIDGE_TOKEN" "http://127.0.0.1:8000/api/status?refresh=1"
curl http://127.0.0.1:8000/api/health
```

返回每个账号的 tier、额度模式、各窗口利用率、剩余比例、保留线、冷却状态与最近错误。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DJANGO_SECRET_KEY` | 开发占位值 | 生产必须修改 |
| `DJANGO_DEBUG` | `1` | 生产设为 `0` |
| `DJANGO_ALLOWED_HOSTS` | 空 | 逗号分隔 |
| `BRIDGE_TOKEN` | 空 | **必填**，为空时拒绝所有代理请求 |
| `BRIDGE_ENCRYPTION_KEY` | 空 | 凭据加密口令 |
| `BRIDGE_RESERVE_RATIO` | `0.05` | 保留比例 |
| `BRIDGE_SAFETY_MARGIN` | `0.005` | 安全余量 |
| `BRIDGE_PROBE_TTL_SECONDS` | `300` | 余额查询缓存 |
| `BRIDGE_PROBE_REQUIRED` | `1` | 无余额数据时禁止路由 |
| `BRIDGE_MAX_ATTEMPTS` | `3` | 单请求最多尝试账号数 |
| `BRIDGE_TIMEOUT_SECONDS` | `600` | 上游超时 |

## 测试

```bash
uv run pytest
```

覆盖：响应头解析、保留线判定、官方/第三方路由顺序、预算累计、余额查询（DeepSeek 结构）、流式转发与用量记录。

## 许可证

本项目基于 [MIT License](LICENSE) 开源。
