# Douyin FastAPI

API 代码位于 `app/src/`，所有路由的前缀是 `/api/v1/douyin`。默认数据目录为 `app/data/`，Cookie 会持久保存，终态任务和事件保留 7 天。

## 服务器启动

```bash
uv sync --extra api --locked
uv run playwright install chromium
uv run uvicorn app.src.main:app --host 0.0.0.0 --port 8000 --workers 1
```

必须使用单个 Uvicorn worker；应用内已提供最多 2 个浏览器任务的并发控制，并且同一账号始终串行。

可用环境变量：

- `SAU_API_DATA_DIR`：持久数据目录。
- `SAU_API_DATABASE_URL`：默认为数据目录下的 SQLite。
- `SAU_API_MAX_BROWSER_TASKS`：默认 `2`。
- `SAU_API_MAX_MATERIAL_TASKS`：异步素材处理并发数，默认 `2`。
- `SAU_API_MAX_CALLBACK_TASKS`：回调投递并发数，默认 `4`。
- `SAU_API_CALLBACK_TIMEOUT_SECONDS`：单次回调超时，默认 `10` 秒。
- `SAU_API_HEADLESS`：默认 `true`。
- `SAU_API_TERMINAL_RETENTION_DAYS`：默认 `7`。

Swagger 中文 API 文档启动后位于 `/docs`，OpenAPI JSON 位于 `/openapi.json`。
所有接口在 Swagger 中统一归类为 `douyin`。健康检查为：

```text
GET /api/v1/douyin/health/live
GET /api/v1/douyin/health/ready
```

## 主要路由

```text
POST   /api/v1/douyin/accounts/login
POST   /api/v1/douyin/accounts/check
POST   /api/v1/douyin/materials
DELETE /api/v1/douyin/materials/{material_id}?account=...
POST   /api/v1/douyin/video
POST   /api/v1/douyin/note
GET    /api/v1/douyin/tasks/{task_id}?account=...
POST   /api/v1/douyin/tasks/{task_id}/verification-code
POST   /api/v1/douyin/tasks/{task_id}/cancel
```

`video` 和 `note` 必须带 `Idempotency-Key` 请求头。旧的 `/publish/video` 和 `/publish/note` 路由不再提供。所有 UUID 均为 32 位小写十六进制字符串，无连字符。

## 同步结果与异步回调

登录、批量素材上传、视频和图文请求均支持可选的 `callback_url`：

- 不传 `callback_url`：接口等待执行完成，直接返回业务结果。
- 传入 HTTP/HTTPS `callback_url`：接口返回 `202` 和 `task_id`，任务在后台执行。
- 发布过程中需要短信验证码时，会返回或回调 `waiting_verification`。提交验证码后任务继续执行。

回调使用 HTTP POST JSON；任意 `2xx` 表示接收成功，不跟随重定向。服务会使用同一个 `event_id` 最多投递 6 次，重试间隔依次为 5 秒、30 秒、2 分钟、10 分钟和 1 小时。待投递事件保存在 SQLite 中，服务重启后会继续投递。任务查询结果中的 `callbacks` 字段可查看投递状态。

## Cookie 登录格式

`POST /api/v1/douyin/accounts/login` 的 `cookie` 字段支持：

1. 浏览器请求头中的原始 Cookie：`sessionid=...; sid_tt=...`。
2. Cookie-Editor 导出数组的 JSON 字符串。
3. Playwright storage_state 对象的 JSON 字符串。

原始 Cookie 请求示例：

```json
{
  "account": "creator_01",
  "cookie": "sessionid=example; sid_tt=example"
}
```

Cookie 等同于账号登录凭据，部署时应通过 HTTPS 传输，不要写入日志或代码仓库。
