from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request

from app.src.api.dependencies import get_container, get_user_id
from app.src.container import AppContainer
from app.src.domain.states import Platform
from app.src.schemas.requests import CheckRequest, LoginRequest, ShipinLoginRequest
from app.src.schemas.responses import ApiErrorEnvelope, ApiSuccessEnvelope, success_response

douyin_router = APIRouter(prefix="/accounts", tags=["douyin"])
shipin_router = APIRouter(prefix="/accounts", tags=["shipin"])


async def _submit_login(
    *,
    user_id: str,
    platform: Platform,
    body: LoginRequest | ShipinLoginRequest,
    request: Request,
    container: AppContainer,
    idempotency_key: str | None,
):
    task, reused = await container.tasks.submit_login(
        user_id, platform, body, idempotency_key
    )
    if body.callback_url is not None:
        return success_response(
            {"task_id": task.id, "status": task.status, "idempotent_replay": reused},
            request.state.request_id,
            status_code=202,
        )
    data, status_code = await container.tasks.wait_for_result(task)
    return success_response(data, request.state.request_id, status_code=status_code)


@douyin_router.post(
    "/login",
    response_model=ApiSuccessEnvelope,
    operation_id="douyin_account_login",
    summary="导入抖音 Cookie",
    responses={
        202: {"model": ApiSuccessEnvelope},
        409: {
            "model": ApiErrorEnvelope,
            "description": "Cookie 无法建立有效抖音登录态，并返回脱敏浏览器诊断",
        },
        422: {"model": ApiErrorEnvelope},
    },
)
async def douyin_login(
    body: LoginRequest,
    request: Request,
    user_id: Annotated[str, Depends(get_user_id)],
    container: Annotated[AppContainer, Depends(get_container)],
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", max_length=128, description="可选的登录任务幂等键"),
    ] = None,
):
    return await _submit_login(
        user_id=user_id,
        platform=Platform.DOUYIN,
        body=body,
        request=request,
        container=container,
        idempotency_key=idempotency_key,
    )


@douyin_router.post(
    "/check",
    response_model=ApiSuccessEnvelope,
    summary="检查抖音登录态",
)
async def douyin_check(
    body: CheckRequest,
    request: Request,
    user_id: Annotated[str, Depends(get_user_id)],
    container: Annotated[AppContainer, Depends(get_container)],
):
    data = await container.accounts.check(user_id, body.account)
    return success_response(data, request.state.request_id)


@shipin_router.post(
    "/login",
    response_model=ApiSuccessEnvelope,
    operation_id="shipin_account_login",
    summary="导入视频号 Cookie",
    description="传入包含 wxuin 和 sessionid 的原始 Cookie，校验成功后按用户和账号保存。",
)
async def shipin_login(
    body: ShipinLoginRequest,
    request: Request,
    user_id: Annotated[str, Depends(get_user_id)],
    container: Annotated[AppContainer, Depends(get_container)],
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", max_length=128, description="可选的登录任务幂等键"),
    ] = None,
):
    return await _submit_login(
        user_id=user_id,
        platform=Platform.SHIPIN,
        body=body,
        request=request,
        container=container,
        idempotency_key=idempotency_key,
    )


@shipin_router.post(
    "/check",
    response_model=ApiSuccessEnvelope,
    summary="检查视频号登录态",
)
async def shipin_check(
    body: CheckRequest,
    request: Request,
    user_id: Annotated[str, Depends(get_user_id)],
    container: Annotated[AppContainer, Depends(get_container)],
):
    data = await container.shipin_accounts.check(user_id, body.account)
    return success_response(data, request.state.request_id)


# Kept as a module-level alias for code that imported the former router directly.
router = douyin_router
