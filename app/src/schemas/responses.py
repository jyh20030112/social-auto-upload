from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field


class ApiSuccessEnvelope(BaseModel):
    """统一成功响应结构。"""

    success: Literal[True] = Field(description="固定为 true，表示请求已成功处理")
    data: Any = Field(description="接口业务数据，具体字段参见对应接口说明")
    request_id: str = Field(
        pattern=r"^[0-9a-f]{32}$",
        description="请求跟踪 ID，32 位小写十六进制 UUID，无连字符",
    )


class ApiErrorDetail(BaseModel):
    """统一错误内容。"""

    code: str = Field(description="稳定的机器可读错误码")
    message: str = Field(description="中文错误说明")
    details: dict[str, Any] = Field(default_factory=dict, description="可选的结构化错误细节")


class ApiErrorEnvelope(BaseModel):
    """统一失败响应结构。"""

    success: Literal[False] = Field(description="固定为 false，表示请求处理失败")
    error: ApiErrorDetail
    request_id: str = Field(
        pattern=r"^[0-9a-f]{32}$",
        description="请求跟踪 ID，用于排查服务端日志",
    )


def iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def success_response(
    data: Any,
    request_id: str,
    *,
    status_code: int = 200,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"success": True, "data": data, "request_id": request_id},
    )


def empty_response(*, status_code: int = 204) -> Response:
    return Response(status_code=status_code)
