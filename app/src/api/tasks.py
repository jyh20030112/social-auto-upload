from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.src.api.dependencies import get_container
from app.src.container import AppContainer
from app.src.schemas.requests import (
    AccountName,
    CancelTaskRequest,
    HexId,
    VerificationCodeRequest,
)
from app.src.schemas.responses import (
    ApiErrorEnvelope,
    ApiSuccessEnvelope,
    success_response,
)

router = APIRouter(prefix="/tasks", tags=["douyin"])


@router.get(
    "/{task_id}",
    response_model=ApiSuccessEnvelope,
    summary="查询抖音任务",
    description=(
        "查询登录、异步素材处理、视频或图文任务的当前状态，并返回最近的进度事件。"
        "使用 callback_url 的任务还会返回各回调事件的投递状态和尝试次数。"
    ),
    response_description="任务状态、执行结果、错误、进度事件和回调投递状态",
    responses={
        404: {"model": ApiErrorEnvelope, "description": "任务不存在或不属于指定账号"},
        422: {"model": ApiErrorEnvelope, "description": "任务 ID 或账号参数无效"},
    },
)
async def get_task(
    task_id: HexId,
    request: Request,
    account: Annotated[AccountName, Query(description="任务所属的抖音账号名称")],
    container: Annotated[AppContainer, Depends(get_container)],
):
    task = await container.tasks.get_for_account(task_id, account)
    data = await container.tasks.serialize(task, include_events=True)
    return success_response(data, request.state.request_id)


@router.post(
    "/{task_id}/verification-code",
    response_model=ApiSuccessEnvelope,
    summary="提交抖音短信验证码",
    description="当发布任务处于 waiting_verification 状态时，提交平台发送的 4—8 位数字验证码。",
    response_description="验证码已被任务接收",
    responses={
        404: {"model": ApiErrorEnvelope, "description": "任务不存在或账号不匹配"},
        409: {"model": ApiErrorEnvelope, "description": "任务未等待验证码，或验证码已提交"},
        422: {"model": ApiErrorEnvelope, "description": "任务 ID、账号或验证码无效"},
    },
)
async def submit_verification_code(
    task_id: HexId,
    body: VerificationCodeRequest,
    request: Request,
    container: Annotated[AppContainer, Depends(get_container)],
):
    await container.tasks.submit_verification_code(task_id, body.account, body.code)
    return success_response({"task_id": task_id, "accepted": True}, request.state.request_id)


@router.post(
    "/{task_id}/cancel",
    response_model=ApiSuccessEnvelope,
    summary="取消抖音任务",
    description=(
        "取消排队中或执行中的任务。如果发布按钮可能已经提交，任务会标记为 "
        "interrupted，表示平台端结果未知。已终止的任务重复取消会直接返回当前状态。"
    ),
    response_description="取消后的任务状态",
    responses={
        404: {"model": ApiErrorEnvelope, "description": "任务不存在或账号不匹配"},
        422: {"model": ApiErrorEnvelope, "description": "任务 ID 或账号参数无效"},
    },
)
async def cancel_task(
    task_id: HexId,
    body: CancelTaskRequest,
    request: Request,
    container: Annotated[AppContainer, Depends(get_container)],
):
    task = await container.tasks.cancel(task_id, body.account)
    return success_response(await container.tasks.serialize(task), request.state.request_id)
