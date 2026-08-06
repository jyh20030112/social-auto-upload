from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request

from app.src.api.dependencies import get_container
from app.src.container import AppContainer
from app.src.schemas.requests import NotePublishRequest, VideoPublishRequest
from app.src.schemas.responses import ApiErrorEnvelope, ApiSuccessEnvelope, success_response


router = APIRouter(prefix="/publish", tags=["douyin"])
IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
        description="发布请求幂等键；同一账号和发布类型下不可复用于不同请求体",
    ),
]


@router.post(
    "/video",
    status_code=202,
    response_model=ApiSuccessEnvelope,
    summary="发布抖音视频",
    description=(
        "根据已上传的视频素材创建异步抖音发布任务。可选横版/竖版封面、"
        "话题、商品链接、自主声明和带时区的定时发布。"
    ),
    response_description="已创建或复用的视频发布任务",
    responses={
        404: {"model": ApiErrorEnvelope, "description": "素材不存在"},
        409: {"model": ApiErrorEnvelope, "description": "未登录或幂等键冲突"},
        422: {"model": ApiErrorEnvelope, "description": "请求参数或素材类型无效"},
    },
)
async def publish_video(
    body: VideoPublishRequest,
    request: Request,
    idempotency_key: IdempotencyKey,
    container: Annotated[AppContainer, Depends(get_container)],
):
    task, reused = await container.tasks.submit_video(body, idempotency_key)
    data = await container.tasks.serialize(task)
    data["idempotent_replay"] = reused
    return success_response(data, request.state.request_id, status_code=202)


@router.post(
    "/note",
    status_code=202,
    response_model=ApiSuccessEnvelope,
    summary="发布抖音图文",
    description=(
        "根据 1—35 张已上传的图片素材创建异步抖音图文发布任务。"
        "支持标题、正文、话题、BGM 和带时区的定时发布。"
    ),
    response_description="已创建或复用的图文发布任务",
    responses={
        404: {"model": ApiErrorEnvelope, "description": "图片素材不存在"},
        409: {"model": ApiErrorEnvelope, "description": "未登录或幂等键冲突"},
        422: {"model": ApiErrorEnvelope, "description": "请求参数或素材类型无效"},
    },
)
async def publish_note(
    body: NotePublishRequest,
    request: Request,
    idempotency_key: IdempotencyKey,
    container: Annotated[AppContainer, Depends(get_container)],
):
    task, reused = await container.tasks.submit_note(body, idempotency_key)
    data = await container.tasks.serialize(task)
    data["idempotent_replay"] = reused
    return success_response(data, request.state.request_id, status_code=202)
