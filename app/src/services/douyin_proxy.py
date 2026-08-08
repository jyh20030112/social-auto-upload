from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Mapping
from urllib.parse import urlsplit

import httpx

if TYPE_CHECKING:
    from app.src.config import Settings


KDL_TPS_ENDPOINT = "https://tps.kdlapi.com/api/gettps"


class DouyinProxyError(RuntimeError):
    """Base error for the Douyin outbound proxy service."""


class ProxyDisabledError(DouyinProxyError):
    pass


class ProxyConfigurationError(DouyinProxyError):
    pass


class ProxyProviderError(DouyinProxyError):
    pass


@dataclass(frozen=True, slots=True, repr=False)
class ProxyConfig:
    enabled: bool = False
    secret_id: str | None = None
    signature: str | None = None
    secret_key: str | None = None
    username: str | None = None
    password: str | None = None
    proxy_auth_mode: str = "basic"
    api_endpoint: str = KDL_TPS_ENDPOINT
    request_timeout_seconds: float = 10.0

    @classmethod
    def from_settings(cls, settings: Settings) -> ProxyConfig:
        return cls(
            enabled=settings.douyin_proxy_enabled,
            secret_id=settings.kdl_secret_id,
            signature=settings.kdl_signature,
            secret_key=settings.kdl_secret_key,
            username=settings.kdl_user_name,
            password=settings.kdl_user_pwd,
            proxy_auth_mode=settings.kdl_proxy_auth_mode,
        )

    @property
    def auth_mode(self) -> str:
        return "hmacsha1" if self.secret_key else "token"

    def validate(self) -> None:
        if not self.enabled:
            return
        if self.proxy_auth_mode not in {"basic", "whitelist"}:
            raise ProxyConfigurationError(
                "KDL_PROXY_AUTH_MODE must be either 'basic' or 'whitelist'"
            )
        missing = [
            name
            for name, value in (("KDL_SECRET_ID", self.secret_id),)
            if not value
        ]
        if self.proxy_auth_mode == "basic":
            missing.extend(
                name
                for name, value in (
                    ("KDL_USER_NAME", self.username),
                    ("KDL_USER_PWD", self.password),
                )
                if not value
            )
        if not self.secret_key and not self.signature:
            missing.append("KDL_SIGNATURE or KDL_SECRET_KEY")
        if missing:
            raise ProxyConfigurationError(
                "Missing required Douyin proxy configuration: " + ", ".join(missing)
            )

    def __repr__(self) -> str:
        return (
            "ProxyConfig("
            f"enabled={self.enabled!r}, auth_mode={self.auth_mode!r}, "
            f"secret_id={'<redacted>' if self.secret_id else None!r}, "
            f"signature={'<redacted>' if self.signature else None!r}, "
            f"secret_key={'<redacted>' if self.secret_key else None!r}, "
            f"username={'<redacted>' if self.username else None!r}, "
            f"password={'<redacted>' if self.password else None!r}, "
            f"proxy_auth_mode={self.proxy_auth_mode!r}, "
            f"api_endpoint={self.api_endpoint!r}, "
            f"request_timeout_seconds={self.request_timeout_seconds!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ProxyEndpoint:
    host: str
    port: int
    username: str | None
    password: str | None

    @property
    def server(self) -> str:
        return f"http://{self.host}:{self.port}"

    def playwright_proxy(self) -> dict[str, str]:
        result = {"server": self.server}
        if self.username and self.password:
            result.update(username=self.username, password=self.password)
        return result

    def __repr__(self) -> str:
        return (
            "ProxyEndpoint("
            f"host={self.host!r}, port={self.port!r}, "
            f"username={'<redacted>' if self.username else None!r}, "
            f"password={'<redacted>' if self.password else None!r})"
        )


class KdlTpsProvider:
    def __init__(
        self,
        config: ProxyConfig,
        *,
        client: httpx.AsyncClient | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=config.request_timeout_seconds,
            follow_redirects=False,
        )

    async def fetch(self) -> ProxyEndpoint:
        self.config.validate()
        params = self._request_params(self._normalized_now())
        try:
            response = await self._client.get(self.config.api_endpoint, params=params)
        except httpx.HTTPError as exc:
            raise ProxyProviderError(
                "KDL TPS request failed before receiving a response"
            ) from exc
        if response.status_code != 200:
            raise ProxyProviderError(
                f"KDL TPS request returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProxyProviderError("KDL TPS returned invalid JSON") from exc
        if not isinstance(payload, Mapping):
            raise ProxyProviderError("KDL TPS returned an unexpected response")
        code = payload.get("code")
        if code not in (0, "0"):
            raise ProxyProviderError(f"KDL TPS returned error code {code!r}")
        data = payload.get("data")
        proxy_list = data.get("proxy_list") if isinstance(data, Mapping) else None
        if not isinstance(proxy_list, list) or len(proxy_list) != 1:
            raise ProxyProviderError(
                "KDL TPS did not return exactly one tunnel endpoint"
            )
        return _parse_proxy_endpoint(
            proxy_list[0],
            username=(
                self.config.username if self.config.proxy_auth_mode == "basic" else None
            ),
            password=(
                self.config.password if self.config.proxy_auth_mode == "basic" else None
            ),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _normalized_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _request_params(self, now: datetime) -> dict[str, str | int]:
        params: dict[str, str | int] = {
            "secret_id": self.config.secret_id or "",
            "sign_type": self.config.auth_mode,
            "num": 1,
            "format": "json",
        }
        if self.config.auth_mode == "token":
            params["signature"] = self.config.signature or ""
            return params

        params["timestamp"] = int(now.timestamp())
        path = urlsplit(self.config.api_endpoint).path
        query = "&".join(f"{key}={params[key]}" for key in sorted(params))
        raw = f"GET{path}?{query}".encode("utf-8")
        digest = hmac.new(
            (self.config.secret_key or "").encode("utf-8"),
            raw,
            hashlib.sha1,
        ).digest()
        params["signature"] = base64.b64encode(digest).decode("ascii")
        return params


class DouyinProxyManager:
    def __init__(
        self,
        config: ProxyConfig,
        *,
        provider: KdlTpsProvider | None = None,
    ) -> None:
        config.validate()
        self.config = config
        self._provider = provider or KdlTpsProvider(config)
        self._endpoints: dict[tuple[str, str], ProxyEndpoint] = {}
        self._locks: defaultdict[tuple[str, str], asyncio.Lock] = defaultdict(
            asyncio.Lock
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> DouyinProxyManager:
        return cls(ProxyConfig.from_settings(settings))

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    async def acquire(self, user_id: str, account: str) -> ProxyEndpoint:
        if not self.enabled:
            raise ProxyDisabledError("Douyin proxy is disabled")

        key = (user_id, account)
        async with self._locks[key]:
            cached = self._endpoints.get(key)
            if cached is not None:
                return cached

            endpoint = await self._provider.fetch()
            self._endpoints[key] = endpoint
            return endpoint

    def invalidate(self, user_id: str, account: str) -> None:
        self._endpoints.pop((user_id, account), None)

    async def aclose(self) -> None:
        await self._provider.aclose()


def _parse_proxy_endpoint(
    item: Any,
    *,
    username: str | None,
    password: str | None,
) -> ProxyEndpoint:
    endpoint: Any = None
    if isinstance(item, str):
        endpoint = item
    elif isinstance(item, Mapping):
        endpoint = item.get("proxy") or item.get("server")
        if not endpoint and item.get("ip") and item.get("port"):
            endpoint = f"{item['ip']}:{item['port']}"
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise ProxyProviderError("KDL TPS tunnel entry has an invalid endpoint")

    endpoint = endpoint.strip()
    if "://" in endpoint:
        parsed = urlsplit(endpoint)
        host = parsed.hostname
        try:
            port = parsed.port
        except ValueError as exc:
            raise ProxyProviderError(
                "KDL TPS tunnel entry has an invalid endpoint"
            ) from exc
    else:
        try:
            host, port_text = endpoint.rsplit(":", 1)
            port = int(port_text)
        except (TypeError, ValueError) as exc:
            raise ProxyProviderError(
                "KDL TPS tunnel entry has an invalid endpoint"
            ) from exc

    if not host or port is None or not 1 <= port <= 65535:
        raise ProxyProviderError("KDL TPS tunnel entry has an invalid endpoint")
    return ProxyEndpoint(
        host=host,
        port=port,
        username=username,
        password=password,
    )
