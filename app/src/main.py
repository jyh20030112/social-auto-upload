from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.src.api import accounts, health, materials, publishing, tasks
from app.src.config import Settings
from app.src.container import build_container
from app.src.domain.errors import ApiError

API_PREFIX = "/api/v1"
logger = logging.getLogger(__name__)

OPENAPI_DESCRIPTION = """

在同一个服务中提供抖音与视频号账号 Cookie 登录、登录态检查、素材管理、内容发布和任务管理。

- 平台路由使用 `/api/v1/douyin` 与 `/api/v1/shipin`，素材、任务和健康检查为通用路由。
- 除健康检查外，所有业务接口必须传 `X-User-ID`，用于隔离 Cookie、素材、任务和幂等键。
- 登录、素材上传、视频和图文接口未传 `callback_url` 时同步等待并直接返回业务结果。
- 传入 `callback_url` 时返回异步任务 ID，并向该 HTTP/HTTPS 地址回调任务事件。
- 遇到短信验证码时返回或回调 `waiting_verification`，通过验证码接口提交后继续执行。
- 视频和图文发布必须传入 `Idempotency-Key` 请求头。
- 所有 ID 均是 32 位小写十六进制 UUID，不含连字符。
- 定时发布时间必须包含时区，并且至少晚于当前时间 2 小时。

回调以 HTTP POST JSON 发送，`event` 为 `waiting_verification` 或任务最终状态，
`event_id` 在重试期间保持不变。任意 `2xx` 表示成功；失败最多投递 6 次，
可通过任务查询接口的 `callbacks` 字段查看投递结果。
"""

OPENAPI_TAGS = [
    {
        "name": "douyin",
        "description": "抖音账号、视频、图文和验证码接口。",
    },
    {"name": "shipin", "description": "视频号账号和视频发布接口。"},
    {"name": "materials", "description": "用户隔离的通用素材接口。"},
    {"name": "tasks", "description": "跨平台通用任务查询与取消接口。"},
    {"name": "health", "description": "无需用户标识的存活与就绪检查。"},
]


def _add_swagger_binary_formats(value: object) -> None:
    """兼容只识别 OpenAPI `format: binary` 的 Swagger UI 版本。"""
    if isinstance(value, dict):
        if (
            value.get("type") == "string"
            and value.get("contentMediaType") == "application/octet-stream"
        ):
            value["format"] = "binary"
        for nested in value.values():
            _add_swagger_binary_formats(nested)
    elif isinstance(value, list):
        for nested in value:
            _add_swagger_binary_formats(nested)


def _error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    details: dict | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "success": False,
            "error": {
                "code": code,
                "message": message,
                "details": details or {},
            },
            "request_id": getattr(request.state, "request_id", uuid4().hex),
        },
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or Settings.from_env()
    container = build_container(resolved_settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        resolved_settings.ensure_directories()
        await container.database.initialize()
        if resolved_settings.worker_enabled:
            interrupted = await container.repository.mark_inflight_interrupted()
            for task in interrupted:
                if task.operation == "upload_materials":
                    container.materials.cleanup_staged(task.payload)
                temporary_cookie_path = task.payload.get("temporary_cookie_path")
                if temporary_cookie_path:
                    Path(temporary_cookie_path).unlink(missing_ok=True)
            await container.callback_worker.start()
            await container.material_worker.start()
            await container.worker.start()
        try:
            yield
        finally:
            if resolved_settings.worker_enabled:
                await container.worker.stop()
                await container.material_worker.stop()
                await container.callback_worker.stop()
            await container.douyin_proxy.aclose()
            await container.database.close()

    application = FastAPI(
        title="自媒体自动发布 API",
        description=OPENAPI_DESCRIPTION,
        version="2.0.0",
        openapi_tags=OPENAPI_TAGS,
        lifespan=lifespan,
    )
    application.state.container = container
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @application.middleware("http")
    async def attach_request_id(request: Request, call_next):
        request.state.request_id = uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @application.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError):
        return _error_response(
            request, exc.status_code, exc.code, exc.message, exc.details
        )

    @application.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError):
        errors = [
            {
                "location": [str(part) for part in error.get("loc", ())],
                "message": error.get("msg", "请求参数错误"),
                "type": error.get("type", "validation_error"),
            }
            for error in exc.errors()
        ]
        return _error_response(
            request,
            422,
            "VALIDATION_ERROR",
            "请求参数校验失败",
            {"errors": errors},
        )

    @application.exception_handler(HTTPException)
    async def handle_http_error(request: Request, exc: HTTPException):
        return _error_response(
            request,
            exc.status_code,
            "HTTP_ERROR",
            str(exc.detail),
        )

    @application.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception):
        logger.exception("Unhandled social publishing API error", exc_info=exc)
        return _error_response(request, 500, "INTERNAL_ERROR", "服务器内部错误")

    application.include_router(accounts.douyin_router, prefix=f"{API_PREFIX}/douyin")
    application.include_router(publishing.douyin_router, prefix=f"{API_PREFIX}/douyin")
    application.include_router(tasks.douyin_router, prefix=f"{API_PREFIX}/douyin")
    application.include_router(accounts.shipin_router, prefix=f"{API_PREFIX}/shipin")
    application.include_router(publishing.shipin_router, prefix=f"{API_PREFIX}/shipin")
    application.include_router(materials.router, prefix=API_PREFIX)
    application.include_router(tasks.router, prefix=API_PREFIX)
    application.include_router(health.router, prefix=API_PREFIX)

    default_openapi = application.openapi

    def swagger_compatible_openapi():
        schema = default_openapi()
        _add_swagger_binary_formats(schema)
        return schema

    application.openapi = swagger_compatible_openapi
    return application


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.src.main:app", host="0.0.0.0", port=8000)
