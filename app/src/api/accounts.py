from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request

from app.src.api.dependencies import get_container
from app.src.container import AppContainer
from app.src.schemas.requests import CheckRequest, LoginRequest
from app.src.schemas.responses import (
    ApiErrorEnvelope,
    ApiSuccessEnvelope,
    success_response,
)

router = APIRouter(prefix="/accounts", tags=["douyin"])


@router.post(
    "/login",
    response_model=ApiSuccessEnvelope,
    operation_id="douyin_account",
    summary="抖音账号",
    description=(
        "接收浏览器请求头中的原始 Cookie 字符串，也支持 Cookie-Editor 导出数组"
        "或 Playwright storage_state 对象的 JSON 字符串。未传 callback_url 时等待校验"
        "完成并直接返回结果；传入 callback_url 时返回任务 ID，并在完成后回调。"
        "校验成功后 Cookie 会按账号长期保存。"
    ),
    response_description="登录结果；异步模式下为任务受理结果",
    responses={
        202: {"model": ApiSuccessEnvelope, "description": "异步任务已受理，或正在等待短信验证码"},
        422: {"model": ApiErrorEnvelope, "description": "Cookie 格式或请求参数无效"},
        500: {"model": ApiErrorEnvelope, "description": "登录校验失败"},
        504: {"model": ApiErrorEnvelope, "description": "登录校验超时"},
    },
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
    if body.callback_url is not None:
        return success_response(
            {
                "task_id": task.id,
                "status": task.status,
                "idempotent_replay": reused,
            },
            request.state.request_id,
            status_code=202,
        )
    data, status_code = await container.tasks.wait_for_result(task)
    return success_response(data, request.state.request_id, status_code=status_code)


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
