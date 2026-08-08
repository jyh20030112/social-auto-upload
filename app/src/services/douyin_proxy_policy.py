from __future__ import annotations

import hashlib

from app.src.domain.errors import ApiError
from app.src.services.douyin_proxy import (
    DouyinProxyError,
    DouyinProxyManager,
    ProxyDisabledError,
    ProxyProviderError,
)
from utils.log import douyin_logger


async def require_douyin_playwright_proxy(
    manager: DouyinProxyManager | None,
    user_id: str,
    account: str,
    operation: str,
) -> dict[str, str]:
    """为抖音 API 获取代理；任何异常都必须中止，禁止回退直连。"""
    try:
        if manager is None or not manager.enabled:
            raise ProxyDisabledError("Douyin proxy is disabled")
        endpoint = await manager.acquire(user_id, account)
        proxy = endpoint.playwright_proxy()
        server = proxy.get("server", "").strip()
        if not server:
            raise ProxyProviderError("Douyin proxy endpoint has no server")
    except ProxyDisabledError as exc:
        raise ApiError(
            503,
            "DOUYIN_PROXY_REQUIRED",
            "抖音登录和发布必须通过代理执行，当前代理未启用",
            {"operation": operation},
        ) from exc
    except DouyinProxyError as exc:
        raise ApiError(
            502,
            "DOUYIN_PROXY_UNAVAILABLE",
            "抖音代理当前不可用，已拒绝回退直连",
            {"operation": operation, "reason": str(exc)},
        ) from exc

    route_id = hashlib.sha256(server.encode("utf-8")).hexdigest()[:12]
    douyin_logger.info(
        f"🔒 抖音{operation}强制使用代理: route_id={route_id}; "
        f"user_id={user_id}; account={account}"
    )
    return proxy
