from __future__ import annotations

from fastapi import Request

from app.src.container import AppContainer


def get_container(request: Request) -> AppContainer:
    return request.app.state.container


def get_request_id(request: Request) -> str:
    return request.state.request_id
