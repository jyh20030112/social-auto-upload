from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.src.api.dependencies import get_container
from app.src.container import AppContainer
from app.src.schemas.responses import ApiSuccessEnvelope, success_response

router = APIRouter(prefix="/health", tags=["health"])


@router.get(
    "/live",
    response_model=ApiSuccessEnvelope,
    summary="API 存活检查",
    description="检查 HTTP 服务进程是否正在运行，不检查数据库和后台任务执行器。",
    response_description="服务进程存活",
)
async def live(request: Request):
    return success_response({"status": "ok"}, request.state.request_id)


@router.get(
    "/ready",
    response_model=ApiSuccessEnvelope,
    summary="API 就绪检查",
    description="检查 SQLite 数据库和后台任务执行器是否就绪，可用于服务器或容器健康检查。",
    response_description="数据库和任务执行器状态",
    responses={
        503: {"model": ApiSuccessEnvelope, "description": "数据库或任务执行器未就绪"}
    },
)
async def ready(
    request: Request,
    container: Annotated[AppContainer, Depends(get_container)],
):
    database_ready = await container.database.ping()
    browser_worker_ready = not container.settings.worker_enabled or container.worker.healthy
    material_worker_ready = (
        not container.settings.worker_enabled or container.material_worker.healthy
    )
    callback_worker_ready = (
        not container.settings.worker_enabled or container.callback_worker.healthy
    )
    ready_state = (
        database_ready
        and browser_worker_ready
        and material_worker_ready
        and callback_worker_ready
    )
    return success_response(
        {
            "status": "ready" if ready_state else "not_ready",
            "database": database_ready,
            "browser_worker": browser_worker_ready,
            "material_worker": material_worker_ready,
            "callback_worker": callback_worker_ready,
        },
        request.state.request_id,
        status_code=200 if ready_state else 503,
    )
