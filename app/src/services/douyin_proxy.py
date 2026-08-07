from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable, Mapping
from urllib.parse import urlsplit

import httpx

if TYPE_CHECKING:
    from app.src.config import Settings


KDL_DPS_ENDPOINT = "https://dps.kdlapi.com/api/getdps"
_CHINA_TIMEZONE = timezone(timedelta(hours=8))


class DouyinProxyError(RuntimeError):
    """Base error for the Douyin outbound proxy service."""


class ProxyDisabledError(DouyinProxyError):
    pass


class ProxyConfigurationError(DouyinProxyError):
    pass


class ProxyProviderError(DouyinProxyError):
    pass


class ProxyTtlError(DouyinProxyError):
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
    api_endpoint: str = KDL_DPS_ENDPOINT
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
            for name, value in (
                ("KDL_SECRET_ID", self.secret_id),
            )
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
class ProxyLease:
    host: str
    port: int
    username: str | None
    password: str | None
    expires_at: datetime

    @property
    def server(self) -> str:
        return f"http://{self.host}:{self.port}"

    def remaining_ttl_seconds(self, now: datetime | None = None) -> float:
        reference = now or datetime.now(timezone.utc)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        return max(0.0, (self.expires_at - reference).total_seconds())

    def playwright_proxy(self) -> dict[str, str]:
        result = {"server": self.server}
        if self.username and self.password:
            result.update(username=self.username, password=self.password)
        return result

    def __repr__(self) -> str:
        return (
            "ProxyLease("
            f"host={self.host!r}, port={self.port!r}, "
            f"username={'<redacted>' if self.username else None!r}, "
            f"password={'<redacted>' if self.password else None!r}, "
            f"expires_at={self.expires_at!r})"
        )


class KdlDpsProvider:
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

    async def fetch(self) -> ProxyLease:
        self.config.validate()
        now = self._normalized_now()
        params = self._request_params(now)
        try:
            response = await self._client.get(self.config.api_endpoint, params=params)
        except httpx.HTTPError as exc:
            raise ProxyProviderError(
                "KDL DPS request failed before receiving a response"
            ) from exc
        if response.status_code != 200:
            raise ProxyProviderError(
                f"KDL DPS request returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProxyProviderError("KDL DPS returned invalid JSON") from exc
        if not isinstance(payload, Mapping):
            raise ProxyProviderError("KDL DPS returned an unexpected response")
        code = payload.get("code")
        if code not in (0, "0"):
            raise ProxyProviderError(f"KDL DPS returned error code {code!r}")
        data = payload.get("data")
        proxy_list = data.get("proxy_list") if isinstance(data, Mapping) else None
        if not isinstance(proxy_list, list) or len(proxy_list) != 1:
            raise ProxyProviderError("KDL DPS did not return exactly one proxy")
        return _parse_proxy_lease(
            proxy_list[0],
            username=(
                self.config.username if self.config.proxy_auth_mode == "basic" else None
            ),
            password=(
                self.config.password if self.config.proxy_auth_mode == "basic" else None
            ),
            now=now,
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
            "f_et": 1,
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
        provider: KdlDpsProvider | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        config.validate()
        self.config = config
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._provider = provider or KdlDpsProvider(config, now=self._now)
        self._leases: dict[tuple[str, str], ProxyLease] = {}
        self._locks: defaultdict[tuple[str, str], asyncio.Lock] = defaultdict(
            asyncio.Lock
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> DouyinProxyManager:
        return cls(ProxyConfig.from_settings(settings))

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    async def acquire(
        self,
        user_id: str,
        account: str,
        minimum_ttl_seconds: int = 300,
    ) -> ProxyLease:
        if not self.enabled:
            raise ProxyDisabledError("Douyin proxy is disabled")
        if minimum_ttl_seconds < 0:
            raise ValueError("minimum_ttl_seconds must not be negative")

        key = (user_id, account)
        async with self._locks[key]:
            now = self._normalized_now()
            cached = self._leases.get(key)
            if cached and cached.remaining_ttl_seconds(now) >= minimum_ttl_seconds:
                return cached

            self._leases.pop(key, None)
            lease = await self._provider.fetch()
            remaining = lease.remaining_ttl_seconds(self._normalized_now())
            if remaining < minimum_ttl_seconds:
                raise ProxyTtlError(
                    "KDL DPS lease TTL is too short: "
                    f"remaining={int(remaining)}s, required={minimum_ttl_seconds}s"
                )
            self._leases[key] = lease
            return lease

    def invalidate(self, user_id: str, account: str) -> None:
        self._leases.pop((user_id, account), None)

    async def aclose(self) -> None:
        await self._provider.aclose()

    def _normalized_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def _parse_proxy_lease(
    item: Any,
    *,
    username: str | None,
    password: str | None,
    now: datetime,
) -> ProxyLease:
    endpoint: Any = None
    expiry: Any = None
    if isinstance(item, str):
        parts = item.rsplit(",", 1)
        if len(parts) != 2:
            raise ProxyProviderError("KDL DPS proxy entry has no expiry value")
        endpoint, expiry = parts
    elif isinstance(item, Mapping):
        endpoint = item.get("proxy") or item.get("server")
        if not endpoint and item.get("ip") and item.get("port"):
            endpoint = f"{item['ip']}:{item['port']}"
        expiry = (
            item.get("expire_time")
            or item.get("expires_at")
            or item.get("ttl")
        )
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise ProxyProviderError("KDL DPS proxy entry has an invalid endpoint")
    try:
        host, port_text = endpoint.strip().rsplit(":", 1)
        port = int(port_text)
    except (TypeError, ValueError) as exc:
        raise ProxyProviderError("KDL DPS proxy entry has an invalid endpoint") from exc
    if not host or not 1 <= port <= 65535:
        raise ProxyProviderError("KDL DPS proxy entry has an invalid endpoint")

    expires_at = _parse_expiry(expiry, now)
    return ProxyLease(
        host=host,
        port=port,
        username=username,
        password=password,
        expires_at=expires_at,
    )


def _parse_expiry(value: Any, now: datetime) -> datetime:
    if value is None:
        raise ProxyProviderError("KDL DPS proxy entry has no expiry value")
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = None
    if numeric is not None:
        if numeric < 0:
            raise ProxyProviderError("KDL DPS proxy entry has an invalid expiry value")
        if numeric >= 1_000_000_000:
            return datetime.fromtimestamp(numeric, tz=timezone.utc)
        return now + timedelta(seconds=numeric)

    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProxyProviderError(
            "KDL DPS proxy entry has an invalid expiry value"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_CHINA_TIMEZONE)
    return parsed.astimezone(timezone.utc)
