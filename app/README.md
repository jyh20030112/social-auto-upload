# 自媒体自动发布 FastAPI

API 代码位于 `app/src/`。同一个应用同时提供抖音和视频号发布能力；素材、任务与健康检查为跨平台通用接口。默认数据目录为 `app/data/`，Cookie 会持久保存，终态任务和事件保留 7 天。

## 服务器启动

```bash
uv sync --extra api --locked
uv run patchright install chromium
uv run uvicorn app.src.main:app --host 0.0.0.0 --port 8000 --workers 1
```

必须使用单个 Uvicorn worker。所有平台共享浏览器任务并发额度，同一 `(user_id, platform, account)` 始终串行。

开发阶段数据库 schema 不做迁移。如果启动提示 schema 版本过旧，请确认不需要旧数据后，手工删除 `app/data/app.db` 再启动；应用不会自动删除数据库或 Cookie。

主要环境变量：

- `SAU_API_DATA_DIR`：持久数据目录。
- `SAU_API_DATABASE_URL`：默认为数据目录下的 SQLite。
- `SAU_API_MAX_BROWSER_TASKS`：抖音和视频号共享的浏览器并发数，默认 `2`。
- `SAU_API_MAX_MATERIAL_TASKS`：异步素材处理并发数，默认 `2`。
- `SAU_API_MAX_CALLBACK_TASKS`：回调投递并发数，默认 `4`。
- `SAU_API_HEADLESS`：抖音浏览器默认无头运行。
- `SAU_API_SHIPIN_HEADLESS`：视频号浏览器默认无头运行。
- `SAU_API_SHIPIN_LOGIN_TIMEOUT_SECONDS`：视频号登录任务超时，默认 `180` 秒。
- `SAU_API_SHIPIN_VIDEO_TIMEOUT_SECONDS`：视频号视频任务超时，默认 `1800` 秒。
- `SAU_API_SHIPIN_PUBLISH_TIMEOUT_SECONDS`：点击一次“发表”后等待平台确认的超时，默认 `120` 秒。
- `SAU_API_SHIPIN_CHECK_TIMEOUT_SECONDS`：视频号登录态检查超时，默认 `90` 秒。
- `SAU_API_TERMINAL_RETENTION_DAYS`：终态任务保留天数，默认 `7`。

Swagger 位于 `/docs`，OpenAPI JSON 位于 `/openapi.json`。

## 用户隔离

除健康检查外，所有业务接口都必须提供：

```text
X-User-ID: user_123
```

格式为 `^[A-Za-z0-9_-]{1,64}$`，区分大小写。当前该字段仅作为隔离键，不是身份鉴权。账号、Cookie、素材、任务、幂等键和回调均按用户隔离。

素材保存到 `app/data/materials/{user_id}/`；Cookie 保存为：

```text
app/data/cookies/{user_id}/douyin_{account}.json
app/data/cookies/{user_id}/shipin_{account}.json
```

## 路由

```text
# 通用接口
POST   /api/v1/materials
DELETE /api/v1/materials/{material_id}
GET    /api/v1/tasks/{task_id}
POST   /api/v1/tasks/{task_id}/cancel
GET    /api/v1/health/live
GET    /api/v1/health/ready

# 抖音
POST   /api/v1/douyin/accounts/login
POST   /api/v1/douyin/accounts/check
POST   /api/v1/douyin/video
POST   /api/v1/douyin/note
POST   /api/v1/douyin/tasks/{task_id}/verification-code

# 视频号
POST   /api/v1/shipin/accounts/login
POST   /api/v1/shipin/accounts/check
POST   /api/v1/shipin/video
```

旧的抖音素材、任务查询/取消和健康检查路径已直接移除。发布接口必须提供 `Idempotency-Key`；其作用域为 `(user_id, platform, account, operation, key)`。

视频号发布请求中的 `short_title` 可省略；传入时长度必须为 6—16 个字符，否则接口直接返回 `422 VALIDATION_ERROR`，不会启动浏览器或上传素材。

## 素材与回调

`POST /api/v1/materials` 使用 multipart/form-data，只包含 `files` 和可选的 `callback_url`，不包含平台或账号。素材在同一用户内按 SHA-256 去重，并可供该用户的所有平台账号使用。

登录、素材上传和发布请求均支持可选 `callback_url`：

- 不传：接口等待任务完成并直接返回结果。
- 传入 HTTP/HTTPS 地址：接口返回 `202` 和 `task_id`，后台处理并回调。
- 回调 JSON 包含 `user_id` 和 `platform`；素材任务的 `platform` 为 `null`。

## 视频号 Cookie 登录

`POST /api/v1/shipin/accounts/login` 请求体示例：

```json
{
  "account": "creator_01",
  "cookie": "wxuin=example;sessionid=example"
}
```

Cookie 会先写入临时文件并校验，只有有效时才原子替换已有 Cookie。Cookie 等同于账号登录凭据，部署时应通过 HTTPS 传输，不要写入日志或代码仓库。
