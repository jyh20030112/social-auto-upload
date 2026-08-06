from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.src.api import accounts, health, materials, publishing, tasks
from app.src.config import Settings
from app.src.container import build_container
from app.src.domain.errors import ApiError


API_PREFIX = "/api/v1/douyin"
logger = logging.getLogger(__name__)

OPENAPI_DESCRIPTION = """
# 抖音自动发布 API

提供抖音账号 Cookie 登录、登录态检查、素材管理、视频/图文发布以及异步任务管理。

- 所有业务路由前缀为 `/api/v1/douyin`。
- 登录和发布接口返回异步任务 ID，通过任务查询接口获取进度。
- 视频和图文发布必须传入 `Idempotency-Key` 请求头。
- 所有 ID 均是 32 位小写十六进制 UUID，不含连字符。
- 定时发布时间必须包含时区，并且至少晚于当前时间 2 小时。
"""

OPENAPI_TAGS = [
    {
        "name": "douyin",
        "description": "抖音账号、素材、内容发布、任务与服务健康检查接口。",
    }
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
            await container.worker.start()
        try:
            yield
        finally:
            if resolved_settings.worker_enabled:
                await container.worker.stop()
            await container.database.close()

    application = FastAPI(
        title="抖音自动发布 API",
        description=OPENAPI_DESCRIPTION,
        version="1.0.0",
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
        return _error_response(request, exc.status_code, exc.code, exc.message, exc.details)

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
        logger.exception("Unhandled Douyin API error", exc_info=exc)
        return _error_response(request, 500, "INTERNAL_ERROR", "服务器内部错误")

    for router in (accounts.router, materials.router, publishing.router, tasks.router, health.router):
        application.include_router(router, prefix=API_PREFIX)

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
