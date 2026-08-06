from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.src.api.dependencies import get_container, get_user_id
from app.src.container import AppContainer
from app.src.schemas.requests import HexId, VerificationCodeRequest
from app.src.schemas.responses import ApiErrorEnvelope, ApiSuccessEnvelope, success_response

router = APIRouter(prefix="/tasks", tags=["tasks"])
douyin_router = APIRouter(prefix="/tasks", tags=["douyin"])


@router.get(
    "/{task_id}",
    response_model=ApiSuccessEnvelope,
    summary="查询任务",
    description="查询当前用户的登录、素材处理或发布任务，并返回最近进度和回调状态。",
    responses={404: {"model": ApiErrorEnvelope}},
)
async def get_task(
    task_id: HexId,
    request: Request,
    user_id: Annotated[str, Depends(get_user_id)],
    container: Annotated[AppContainer, Depends(get_container)],
):
    task = await container.tasks.get_for_user(task_id, user_id)
    return success_response(
        await container.tasks.serialize(task, include_events=True),
        request.state.request_id,
    )


@router.post(
    "/{task_id}/cancel",
    response_model=ApiSuccessEnvelope,
    summary="取消任务",
    description="取消当前用户的任务；不需要请求体。",
    responses={404: {"model": ApiErrorEnvelope}},
)
async def cancel_task(
    task_id: HexId,
    request: Request,
    user_id: Annotated[str, Depends(get_user_id)],
    container: Annotated[AppContainer, Depends(get_container)],
):
    task = await container.tasks.cancel(task_id, user_id)
    return success_response(await container.tasks.serialize(task), request.state.request_id)


@douyin_router.post(
    "/{task_id}/verification-code",
    response_model=ApiSuccessEnvelope,
    summary="提交抖音短信验证码",
    description="仅接受当前用户的抖音 waiting_verification 任务。请求体只包含 code。",
    responses={
        404: {"model": ApiErrorEnvelope},
        409: {"model": ApiErrorEnvelope},
    },
)
async def submit_verification_code(
    task_id: HexId,
    body: VerificationCodeRequest,
    request: Request,
    user_id: Annotated[str, Depends(get_user_id)],
    container: Annotated[AppContainer, Depends(get_container)],
):
    await container.tasks.submit_verification_code(task_id, user_id, body.code)
    return success_response({"task_id": task_id, "accepted": True}, request.state.request_id)
