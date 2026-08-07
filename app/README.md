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
- `SAU_API_DOUYIN_PROXY_ENABLED`：是否为抖音登录、鉴权和发布启用快代理 DPS，默认 `false`。
- `SAU_API_TERMINAL_RETENTION_DAYS`：终态任务保留天数，默认 `7`。

Swagger 位于 `/docs`，OpenAPI JSON 位于 `/openapi.json`。

## 抖音快代理 DPS

项目支持为每个 `(X-User-ID, account)` 获取并复用一条快代理 DPS 租约，抖音登录、登录态检查、视频发布和图文发布会使用同一套 Playwright 代理参数。复制 `.env.example` 为仓库根目录的 `.env`，填入快代理订单凭据后再配置认证模式：

```dotenv
SAU_API_DOUYIN_PROXY_ENABLED=false
KDL_SECRET_ID=your_secret_id
KDL_SIGNATURE=your_token
KDL_SECRET_KEY=
KDL_PROXY_AUTH_MODE=whitelist
KDL_USER_NAME=
KDL_USER_PWD=
```

- `KDL_SIGNATURE` 是 API token；如果改填 `KDL_SECRET_KEY`，提取接口会使用 HMAC-SHA1 签名。
- `KDL_PROXY_AUTH_MODE=whitelist` 适合有固定公网出口 IP 的服务器，必须先在快代理订单中加入该 IP。
- `KDL_PROXY_AUTH_MODE=basic` 会把 `KDL_USER_NAME` 和 `KDL_USER_PWD` 交给 Playwright。当前实测 Chromium 对该代理的 HTTPS CONNECT 返回 `ERR_TUNNEL_CONNECTION_FAILED`，服务器部署优先使用白名单。
- `.env` 已被 Git 忽略；不要把凭据、完整代理地址或 Cookie 写入日志和仓库。

启用前先做只读诊断。它只比较直连/代理出口并打开创作者上传页，不会选择素材、点击发布或改写 Cookie：

```bash
uv run python scripts/diagnose_douyin_proxy.py \
  --storage-state app/data/cookies/<user_id>/douyin_<account>.json \
  --account <account> \
  --json-output /tmp/douyin-proxy-diagnostic.json
```

只有诊断返回 `proxy_may_help` 才表示代理路径具备继续灰度测试的条件；其他结论均保持 `SAU_API_DOUYIN_PROXY_ENABLED=false`。诊断 JSON 权限为 `0600`，终端中的出口 IP 会被脱敏。

当前 DPS 订单实测租约约为 5—10 分钟，而应用默认要求视频租约至少覆盖 `SAU_API_VIDEO_TIMEOUT_SECONDS + 300` 秒、图文至少覆盖 `SAU_API_NOTE_TIMEOUT_SECONDS + 300` 秒。租约不足时任务会在启动浏览器前明确失败，避免发布途中换 IP。若要正式发布，需要购买/配置更长存活时间的代理产品，或相应缩短业务超时并完成真实素材灰度验证；单纯启用当前短租约不能保证绕过抖音风控。

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
