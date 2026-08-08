from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request

from app.src.api.dependencies import get_container, get_user_id
from app.src.container import AppContainer
from app.src.domain.states import Platform
from app.src.schemas.requests import (
    NotePublishRequest,
    ShipinVideoPublishRequest,
    VideoPublishRequest,
)
from app.src.schemas.responses import ApiErrorEnvelope, ApiSuccessEnvelope, success_response

douyin_router = APIRouter(tags=["douyin"])
shipin_router = APIRouter(tags=["shipin"])
IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
        description="发布请求幂等键；同一用户、平台、账号和发布类型下不可复用于不同请求体",
    ),
]


async def _publish_response(body, task, reused: bool, request: Request, container: AppContainer):
    if body.callback_url is not None:
        return success_response(
            {"task_id": task.id, "status": task.status, "idempotent_replay": reused},
            request.state.request_id,
            status_code=202,
        )
    data, status_code = await container.tasks.wait_for_result(task)
    return success_response(data, request.state.request_id, status_code=status_code)


@douyin_router.post(
    "/video",
    response_model=ApiSuccessEnvelope,
    operation_id="douyin_video",
    summary="发布抖音视频",
    description=(
        "可选在请求体中传入 cookie；传入后，服务会在同一个代理浏览器上下文中完成"
        "登录态确认、视频上传和发布，并在稳定页面跳转后刷新长期 Cookie。"
    ),
    responses={
        202: {"model": ApiSuccessEnvelope},
        404: {"model": ApiErrorEnvelope},
        409: {"model": ApiErrorEnvelope},
        422: {"model": ApiErrorEnvelope},
        502: {"model": ApiErrorEnvelope},
        503: {"model": ApiErrorEnvelope},
    },
)
async def publish_douyin_video(
    body: VideoPublishRequest,
    request: Request,
    user_id: Annotated[str, Depends(get_user_id)],
    idempotency_key: IdempotencyKey,
    container: Annotated[AppContainer, Depends(get_container)],
):
    task, reused = await container.tasks.submit_video(
        user_id, Platform.DOUYIN, body, idempotency_key
    )
    return await _publish_response(body, task, reused, request, container)


@douyin_router.post(
    "/note",
    response_model=ApiSuccessEnvelope,
    operation_id="douyin_note",
    summary="发布抖音图文",
)
async def publish_douyin_note(
    body: NotePublishRequest,
    request: Request,
    user_id: Annotated[str, Depends(get_user_id)],
    idempotency_key: IdempotencyKey,
    container: Annotated[AppContainer, Depends(get_container)],
):
    task, reused = await container.tasks.submit_note(user_id, body, idempotency_key)
    return await _publish_response(body, task, reused, request, container)


@shipin_router.post(
    "/video",
    response_model=ApiSuccessEnvelope,
    operation_id="shipin_video",
    summary="发布视频号视频",
    description="复用视频号 CLI 发布能力；支持双封面、短标题、分类和定时发布，不支持草稿。",
)
async def publish_shipin_video(
    body: ShipinVideoPublishRequest,
    request: Request,
    user_id: Annotated[str, Depends(get_user_id)],
    idempotency_key: IdempotencyKey,
    container: Annotated[AppContainer, Depends(get_container)],
):
    task, reused = await container.tasks.submit_video(
        user_id, Platform.SHIPIN, body, idempotency_key
    )
    return await _publish_response(body, task, reused, request, container)


router = douyin_router
