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
| 官方 ChatGPT (Codex) | `official` | `oauth` | `official` | 从 `~/.codex/auth.json` 读 token，按 `wham/usage` 判定 95% |
| 第三方供应商 | `third_party` | `api_key` | `probe` | 填 `base_url` 与余额查询配置；`channel` 可填渠道标签；`reserve_ratio` 建议设 `0` |

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

默认策略是**官方账号保护、第三方不保护**：

| tier | `reserve_ratio` | `safety_margin` | 行为 |
| --- | --- | --- | --- |
| `official` | `0.05` | `0.005` | 用到 94.5% 即停，保留最后 5% |
| `third_party` | `0` | `0` | 不预留，余额耗尽（利用率 100%）才跳过 |

```
limit = 1 - reserve_ratio - safety_margin
```

- 任一窗口（5h / 7d / 预算 / 余额）利用率 ≥ `limit`，账号即被跳过
- 官方账号全部到线 → 使用第三方供应商；第三方余额也耗尽 → `429 rate_limit_error`
- `inflight` 达到 `max_inflight` 的账号暂时不再接新请求，减少并发越线
- `probe` 模式必须先成功查到一次余额（`BRIDGE_PROBE_REQUIRED=1`），否则不会路由，避免“额度未知却继续花”
- 新建账号的 `BRIDGE_RESERVE_RATIO` 只是默认值，第三方账号建议在后台显式改为 `0`

### 官方 ChatGPT 账号（Codex）

`protocol=codex` 的账号会代理到官方 `https://chatgpt.com/backend-api/codex`，额度取自
`https://chatgpt.com/backend-api/wham/usage` 的 `primary_window / secondary_window.used_percent`：

- 任一窗口 ≥ 94.5% 即跳过该账号，自动切到第三方供应商
- 未取得额度数据前默认**不可用**（fail-closed），避免误耗官方额度
- token 直接从 `~/.codex/auth.json` 读取，不复制进桥数据库；登录刷新后自动生效
- 建议把官方账号设为 `tier=official, priority=1`，第三方设为 `tier=third_party`

> 该策略按上游响应头/官方用量接口判定并预留 0.5% 安全余量；极端并发下仍可能因响应滞后出现轻微越线。

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
curl "http://127.0.0.1:8000/api/logs?limit=50" -H "x-bridge-token: $BRIDGE_TOKEN"
curl "http://127.0.0.1:8000/api/logs?channel=pp.dog" -H "x-bridge-token: $BRIDGE_TOKEN"
curl http://127.0.0.1:8000/api/health
```

请求日志按 `channel`（渠道标签）、`account`、`tier`、`base_url` 分开记录，可通过 `channel` / `account` 参数过滤。

### 镜像到 CC Switch

设置 `BRIDGE_CC_SWITCH_MIRROR=1`（默认开启）后，桥会把 CC Switch 原生代理日志中那条“桥”的记录，改写成真实供应商的 `provider_id`：

- 保留 CC Switch 自己采集的 token、成本、速度、首字时间
- 按模型、token 数、时间窗口匹配原生记录，匹配失败时退化为写入一条桥侧日志
- `cc_provider_id` 在账号编辑页填写，对应 CC Switch 的 `providers.id`
- 需要把 CC Switch 里桥的 provider id 配到 `BRIDGE_CC_SWITCH_BRIDGE_PROVIDER_ID`
- 写入/改写是 best-effort，CC Switch 数据库不可用时不影响代理
渠道标签在账号编辑页填写；注意它只用于桥侧日志区分，中转站内部的渠道对客户端不可见。

返回每个账号的 tier、额度模式、各窗口利用率、剩余比例、保留线、冷却状态与最近错误。

## 动态路由

当 `BRIDGE_ROUTING_MODE=dynamic`（默认）时，桥会在同一 tier 内按以下指标打分选渠道，而不是固定优先级：

- **价格**：优先使用供应商 `/v1/usage` 里 `model_stats` 的真实成本折算每千 token 单价（`observed_cost_per_1k`），未采集到时才用 `price_multiplier` × 模型价格
- **首字延迟**：流式请求的首个内容事件耗时 EWMA
- **生成速度**：输出 token / 生成耗时 EWMA
- **错误率**：成功率的 EWMA
- **探索**：样本不足的渠道有 15% 分数加成，避免一开始就被冷落

速度优先、兼顾价格：默认权重 `TTFT 0.50 / 速度 0.25 / 价格 0.20 / 错误率 0.05`，优先级权重为 0。

- 首字超过 `BRIDGE_SLOW_TTFT_MS`（默认 10000ms）或生成速度低于 `BRIDGE_SLOW_MIN_TPS`（默认 15）会标记为慢请求，**下一请求立即跳过该渠道 600 秒**；到期后会重新参与，不会被永久拉黑
- 在评分接近的候选里按「最久未使用」轮换，并有 `BRIDGE_EXPLORE_RATE`（默认 0.3）概率随机探索
- 同一 tier 内才做动态比较；官方账号仍优先且受 95% 保留线约束
- 若某渠道返回「模型不可用」，该账号会被加入 `blocked_models`，本请求自动换下一个渠道

## 会话看门狗

`scripts/codex_session_watchdog.py` 用于托管一个 Codex 任务会话，区分「任务完成」与「异常停止」：

```bash
.venv/bin/python scripts/codex_session_watchdog.py \
  --prompt "完成 xxx 任务" \
  --cwd /path/to/workspace \
  --max-restarts 3 \
  --log .run/watchdog.log --state .run/watchdog-state.json
```

- 退出码 `0` 且未出现失败标记 → `completed`，不再重启
- 非 0 退出、被信号终止、或输出出现 `exceeded retry limit` → `abnormal_stop`
- 异常时用 `codex exec resume <session_id>`（没有 id 时 `--last`）继续，指数退避，超过 `--max-restarts` 标记 `failed`
- 也支持 `--complete-marker TASK_COMPLETE`，由任务显式声明完成
- 状态文件记录 `attempts / status / exit_code / session_id`，日志为 JSON Lines

> 看门狗只能托管**由它启动**的会话；已经在跑的交互式会话无法中途接管。

## 生产部署

### 直接运行

```bash
uv sync --frozen --no-dev
cp .env.example .env          # 设 DJANGO_DEBUG=0，并修改 SECRET_KEY / BRIDGE_TOKEN
uv run python manage.py migrate
uv run python manage.py collectstatic --noinput
uv run uvicorn cc_switch.asgi:application --host 127.0.0.1 --port 8000 --workers 1
```

- 内置 SQLite 与按进程的并发计数，建议 `--workers 1`；需要横向扩展时改用 PostgreSQL 并复核并发控制
- 生产环境必须设置 `DJANGO_ALLOWED_HOSTS`，HTTPS 域名还要设置 `DJANGO_CSRF_TRUSTED_ORIGINS`

### Nginx 反向代理（SSE 关键配置）

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_buffering off;          # 必须关闭，否则流式响应会被缓冲
    proxy_read_timeout 600s;
}
```

### systemd

```ini
[Unit]
Description=CC Switch bridge
After=network.target

[Service]
WorkingDirectory=/opt/cc-switch
EnvironmentFile=/opt/cc-switch/.env
ExecStart=/opt/cc-switch/.venv/bin/uvicorn cc_switch.asgi:application --host 127.0.0.1 --port 8000 --workers 1
Restart=always

[Install]
WantedBy=multi-user.target
```

### Docker Compose

```bash
cp .env.example .env
docker compose up -d --build
docker compose logs -f
```

数据库持久化在 `bridge-data` 卷中；升级时重新 `docker compose up -d --build` 即可，容器启动会自动执行迁移。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DJANGO_SECRET_KEY` | 开发占位值 | 生产必须修改 |
| `DJANGO_DEBUG` | `1` | 生产设为 `0` |
| `DJANGO_ALLOWED_HOSTS` | 空 | 逗号分隔 |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | 空 | 部署 HTTPS 管理后台时填写 |
| `DJANGO_DB_PATH` | `db.sqlite3` | SQLite 文件路径，容器内为 `/data/db.sqlite3` |
| `BRIDGE_TOKEN` | 空 | **必填**，为空时拒绝所有代理请求 |
| `BRIDGE_ENCRYPTION_KEY` | 空 | 凭据加密口令 |
| `BRIDGE_RESERVE_RATIO` | `0.05` | 新建账号的默认保留比例（官方 0.05，第三方建议 0） |
| `BRIDGE_SAFETY_MARGIN` | `0.005` | 安全余量 |
| `BRIDGE_PROBE_TTL_SECONDS` | `300` | 余额查询缓存 |
| `BRIDGE_PROBE_REQUIRED` | `1` | 无余额数据时禁止路由 |
| `BRIDGE_CODEX_AUTH_PATH` | `~/.codex/auth.json` | 官方 ChatGPT 登录文件 |
| `BRIDGE_OFFICIAL_QUOTA_URL` | `https://chatgpt.com/backend-api/wham/usage` | 官方额度接口 |
| `BRIDGE_OFFICIAL_QUOTA_TTL_SECONDS` | `60` | 官方额度缓存时间 |
| `BRIDGE_ROUTING_MODE` | `dynamic` | `dynamic` 动态评分 / `priority` 固定优先级 |
| `BRIDGE_WEIGHT_COST` / `WEIGHT_TTFT` / `WEIGHT_SPEED` / `WEIGHT_ERROR` | `0.40/0.30/0.20/0.10` | 动态评分权重 |
| `BRIDGE_WEIGHT_PRIORITY` | `0.05` | 优先级在评分中的权重 |
| `BRIDGE_STATS_MIN_SAMPLES` | `3` | 低于该样本数时给予探索加成 |
| `BRIDGE_SLOW_TTFT_MS` / `BRIDGE_SLOW_MIN_TPS` | `10000` / `15` | 慢请求判定阈值 |
| `BRIDGE_SLOW_PENALTY_SECONDS` | `600` | 慢请求后跳过该渠道的秒数 |
| `BRIDGE_ROTATION_MARGIN` | `0.5` | 相近候选的轮换范围 |
| `BRIDGE_EXPLORE_RATE` | `0.1` | 随机探索概率 |
| `BRIDGE_ROUTING_ALPHA` | `0.6` | 指标 EWMA 响应速度 |
| `BRIDGE_CC_SWITCH_MIRROR` | `1` | 把请求日志镜像到 CC Switch |
| `BRIDGE_CC_SWITCH_DB_PATH` | `~/.cc-switch/cc-switch.db` | CC Switch 数据库路径 |
| `BRIDGE_MAX_ATTEMPTS` | `100` | 单请求最多尝试次数（多轮循环，10 个账号约 10 轮） |
| `BRIDGE_RETRY_ROUND_DELAY` | `0.5` | 每轮之间的等待秒数 |
| `BRIDGE_TIMEOUT_SECONDS` | `600` | 上游超时 |

## 测试

```bash
uv run pytest
```

覆盖：响应头解析、保留线判定、官方/第三方路由顺序、预算累计、余额查询（DeepSeek 结构）、流式转发与用量记录。

## 许可证

本项目基于 [MIT License](LICENSE) 开源。
