from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request

from app.src.api.dependencies import get_container
from app.src.container import AppContainer
from app.src.schemas.requests import CheckRequest, LoginRequest
from app.src.schemas.responses import ApiErrorEnvelope, ApiSuccessEnvelope, success_response


router = APIRouter(prefix="/accounts", tags=["douyin"])


@router.post(
    "/login",
    status_code=202,
    response_model=ApiSuccessEnvelope,
    summary="导入 Cookie 并登录抖音账号",
    description=(
        "接收浏览器请求头中的原始 Cookie 字符串，也支持 Cookie-Editor 导出数组"
        "或 Playwright storage_state 对象的 JSON 字符串。接口创建异步登录任务，"
        "校验成功后 Cookie 会按账号长期保存。"
    ),
    response_description="已创建登录任务",
    responses={422: {"model": ApiErrorEnvelope, "description": "Cookie 格式或请求参数无效"}},
)
async def login(
    body: LoginRequest,
    request: Request,
    container: Annotated[AppContainer, Depends(get_container)],
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            max_length=128,
            description="可选的登录任务幂等键",
        ),
    ] = None,
):
    task, reused = await container.tasks.submit_login(body, idempotency_key)
    data = await container.tasks.serialize(task)
    data["idempotent_replay"] = reused
    return success_response(data, request.state.request_id, status_code=202)


@router.post(
    "/check",
    response_model=ApiSuccessEnvelope,
    summary="检查抖音登录态",
    description="同步调用抖音 Cookie 鉴权逻辑，检查指定账号的已保存 Cookie 是否有效。",
    response_description="账号登录状态",
    responses={
        422: {"model": ApiErrorEnvelope, "description": "请求参数无效"},
        504: {"model": ApiErrorEnvelope, "description": "抖音登录态检查超时"},
    },
)
async def check(
    body: CheckRequest,
    request: Request,
    container: Annotated[AppContainer, Depends(get_container)],
):
    data = await container.accounts.check(body.account)
    return success_response(data, request.state.request_id)
