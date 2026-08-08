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
- `SAU_API_DEBUG`：是否在抖音 Cookie 校验最终失败时保存诊断截图，默认 `false`。
- `SAU_API_SHIPIN_HEADLESS`：视频号浏览器默认无头运行。
- `SAU_API_SHIPIN_LOGIN_TIMEOUT_SECONDS`：视频号登录任务超时，默认 `180` 秒。
- `SAU_API_SHIPIN_VIDEO_TIMEOUT_SECONDS`：视频号视频任务超时，默认 `1800` 秒。
- `SAU_API_SHIPIN_PUBLISH_TIMEOUT_SECONDS`：点击一次“发表”后等待平台确认的超时，默认 `120` 秒。
- `SAU_API_SHIPIN_CHECK_TIMEOUT_SECONDS`：视频号登录态检查超时，默认 `90` 秒。
- `SAU_API_DOUYIN_PROXY_ENABLED`：是否为抖音登录、鉴权和发布启用快代理 TPS 隧道，默认 `false`。
- `SAU_API_TERMINAL_RETENTION_DAYS`：终态任务保留天数，默认 `7`。

Swagger 位于 `/docs`，OpenAPI JSON 位于 `/openapi.json`。

## 抖音快代理 TPS 隧道

项目通过快代理 TPS 接口获取隧道地址，并按 `(X-User-ID, account)` 缓存。抖音登录、登录态检查、视频发布和图文发布都会使用同一套 Playwright 代理参数。TPS 隧道没有 DPS 的单条代理过期时间，因此应用不再根据任务超时申请或校验租约 TTL。复制 `.env.example` 为仓库根目录的 `.env`，填入快代理订单凭据后再配置认证模式：

```dotenv
SAU_API_DOUYIN_PROXY_ENABLED=false
KDL_SECRET_ID=your_secret_id
KDL_SIGNATURE=
KDL_SECRET_KEY=your_secret_key
KDL_PROXY_AUTH_MODE=whitelist
KDL_USER_NAME=
KDL_USER_PWD=
```

- `KDL_SIGNATURE` 是 API token；如果改填 `KDL_SECRET_KEY`，提取接口会使用 HMAC-SHA1 签名。
- `KDL_SECRET_ID`/`KDL_SECRET_KEY` 用于调用 TPS 提取接口；控制台中的“API 授权白名单”也只保护该接口。
- `KDL_PROXY_AUTH_MODE=whitelist` 适合有固定公网出口 IP 的服务器，还必须把该 IP 加入 TPS 订单的“代理访问白名单”。API 授权白名单不能替代代理访问白名单。
- `KDL_PROXY_AUTH_MODE=basic` 会把 TPS 订单的 `KDL_USER_NAME` 和 `KDL_USER_PWD` 交给 Playwright；它们不是 SecretId/SecretKey。
- `.env` 已被 Git 忽略；不要把凭据、完整代理地址或 Cookie 写入日志和仓库。

启用前先做只读诊断。它只比较直连/代理出口并打开创作者上传页，不会选择素材、点击发布或改写 Cookie：

```bash
uv run python scripts/diagnose_douyin_proxy.py \
  --storage-state app/data/cookies/<user_id>/douyin_<account>.json \
  --account <account> \
  --json-output /tmp/douyin-proxy-diagnostic.json
```

只有诊断返回 `proxy_may_help` 才表示代理路径具备继续灰度测试的条件；其他结论均保持 `SAU_API_DOUYIN_PROXY_ENABLED=false`。诊断 JSON 权限为 `0600`，终端中的出口 IP 会被脱敏，并用 `proxy_tunnel.endpoint_acquired` 标记 TPS 隧道地址是否成功获取。

隧道地址稳定不代表出口 IP 永远不变。抖音对登录 IP 敏感，TPS 订单应配置足够长的粘性/换 IP 周期，使一次登录检查或发布任务期间出口保持稳定；应用不会在任务中主动调用换 IP。正式启用前仍应使用真实账号和素材灰度验证。

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

## 抖音 Cookie 登录诊断

抖音 Cookie 校验会把每次 Patchright 检查到的脱敏现场写入 `logs/douyin.log`，包括尝试次数、最终 URL（不含查询参数）、页面标题、登录页特征、文件上传框数量、截断后的可见文本和浏览器异常。手机号、Cookie、Token、签名及 URL 中的代理凭据会在写日志和返回 API 前脱敏。

登录失败时接口返回 `409 DOUYIN_COOKIE_INVALID`，`details.browser_diagnostic` 包含最后一次浏览器现场，`details.task_id` 可用于关联任务和日志。设置 `SAU_API_DEBUG=true` 后，只有最后一次失败会在 `app/data/tmp/diagnostics/`（或自定义 `SAU_API_DATA_DIR` 下的对应目录）保存权限为 `0600` 的全页截图；截图可能包含页面信息，应按敏感文件管理并定期清理。
