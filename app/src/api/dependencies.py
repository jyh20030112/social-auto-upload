from __future__ import annotations

from typing import Annotated

from fastapi import Header, Request

from app.src.container import AppContainer
from app.src.schemas.requests import UserId


def get_container(request: Request) -> AppContainer:
    return request.app.state.container


def get_request_id(request: Request) -> str:
    return request.state.request_id


def get_user_id(
    user_id: Annotated[
        UserId,
        Header(alias="X-User-ID", description="调用方用户标识，用于隔离账号、素材和任务"),
    ],
) -> str:
    return user_id
