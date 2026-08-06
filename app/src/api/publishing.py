from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request

from app.src.api.dependencies import get_container
from app.src.container import AppContainer
from app.src.schemas.requests import NotePublishRequest, VideoPublishRequest
from app.src.schemas.responses import (
    ApiErrorEnvelope,
    ApiSuccessEnvelope,
    success_response,
)

router = APIRouter(tags=["douyin"])
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
    response_model=ApiSuccessEnvelope,
    operation_id="douyin_video",
    summary="抖音视频",
    description=(
        "根据已上传的视频素材发布抖音视频。未传 callback_url 时等待发布完成并"
        "直接返回结果；传入 callback_url 时返回任务 ID，并回调验证码等待和最终结果。"
        "可选横版/竖版封面、话题、商品链接、自主声明和带时区的定时发布。"
    ),
    response_description="视频发布结果；异步模式下为任务受理结果",
    responses={
        202: {"model": ApiSuccessEnvelope, "description": "异步任务已受理，或正在等待短信验证码"},
        404: {"model": ApiErrorEnvelope, "description": "素材不存在"},
        409: {"model": ApiErrorEnvelope, "description": "未登录或幂等键冲突"},
        422: {"model": ApiErrorEnvelope, "description": "请求参数或素材类型无效"},
        500: {"model": ApiErrorEnvelope, "description": "视频发布失败"},
        504: {"model": ApiErrorEnvelope, "description": "视频发布超时"},
    },
)
async def publish_video(
    body: VideoPublishRequest,
    request: Request,
    idempotency_key: IdempotencyKey,
    container: Annotated[AppContainer, Depends(get_container)],
):
    task, reused = await container.tasks.submit_video(body, idempotency_key)
    if body.callback_url is not None:
        return success_response(
            {"task_id": task.id, "status": task.status, "idempotent_replay": reused},
            request.state.request_id,
            status_code=202,
        )
    data, status_code = await container.tasks.wait_for_result(task)
    return success_response(data, request.state.request_id, status_code=status_code)


@router.post(
    "/note",
    response_model=ApiSuccessEnvelope,
    operation_id="douyin_note",
    summary="抖音图文",
    description=(
        "根据 1—35 张已上传的图片素材发布抖音图文。未传 callback_url 时等待"
        "发布完成并直接返回结果；传入 callback_url 时返回任务 ID，并回调验证码"
        "等待和最终结果。支持标题、正文、话题、BGM 和带时区的定时发布。"
    ),
    response_description="图文发布结果；异步模式下为任务受理结果",
    responses={
        202: {"model": ApiSuccessEnvelope, "description": "异步任务已受理，或正在等待短信验证码"},
        404: {"model": ApiErrorEnvelope, "description": "图片素材不存在"},
        409: {"model": ApiErrorEnvelope, "description": "未登录或幂等键冲突"},
        422: {"model": ApiErrorEnvelope, "description": "请求参数或素材类型无效"},
        500: {"model": ApiErrorEnvelope, "description": "图文发布失败"},
        504: {"model": ApiErrorEnvelope, "description": "图文发布超时"},
    },
)
async def publish_note(
    body: NotePublishRequest,
    request: Request,
    idempotency_key: IdempotencyKey,
    container: Annotated[AppContainer, Depends(get_container)],
):
    task, reused = await container.tasks.submit_note(body, idempotency_key)
    if body.callback_url is not None:
        return success_response(
            {"task_id": task.id, "status": task.status, "idempotent_replay": reused},
            request.state.request_id,
            status_code=202,
        )
    data, status_code = await container.tasks.wait_for_result(task)
    return success_response(data, request.state.request_id, status_code=status_code)
