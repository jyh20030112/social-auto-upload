from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

from app.src.config import Settings
from app.src.services.douyin_proxy import (
    DouyinProxyManager,
    KdlDpsProvider,
    ProxyConfig,
    ProxyConfigurationError,
    ProxyDisabledError,
    ProxyLease,
    ProxyProviderError,
    ProxyTtlError,
)


NOW = datetime(2024, 1, 1, tzinfo=timezone.utc)


class SettingsProxyTests(unittest.TestCase):
    def test_from_env_loads_dotenv_without_overriding_process_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            environment = {
                "SAU_API_DATA_DIR": str(data_dir),
                "SAU_API_DOUYIN_PROXY_ENABLED": "true",
                "KDL_SECRET_ID": "process-secret-id",
                "KDL_SIGNATURE": "token",
                "KDL_USER_NAME": "proxy-user",
                "KDL_USER_PWD": "proxy-password",
                "KDL_PROXY_AUTH_MODE": "whitelist",
            }
            with (
                patch.dict("os.environ", environment, clear=True),
                patch("app.src.config.load_dotenv") as load_dotenv,
            ):
                settings = Settings.from_env()

        load_dotenv.assert_called_once()
        self.assertFalse(load_dotenv.call_args.kwargs["override"])
        self.assertTrue(settings.douyin_proxy_enabled)
        self.assertEqual(settings.kdl_secret_id, "process-secret-id")
        self.assertEqual(settings.kdl_signature, "token")
        self.assertEqual(settings.kdl_user_name, "proxy-user")
        self.assertEqual(settings.kdl_user_pwd, "proxy-password")
        self.assertEqual(settings.kdl_proxy_auth_mode, "whitelist")


class ProxyValueTests(unittest.TestCase):
    def test_sensitive_values_are_redacted_from_repr(self):
        config = ProxyConfig(
            enabled=True,
            secret_id="secret-id-value",
            signature="signature-value",
            secret_key="secret-key-value",
            username="username-value",
            password="password-value",
        )
        lease = ProxyLease(
            host="127.0.0.1",
            port=8080,
            username="username-value",
            password="password-value",
            expires_at=NOW + timedelta(minutes=5),
        )

        rendered = repr(config) + repr(lease)
        for secret in (
            "secret-id-value",
            "signature-value",
            "secret-key-value",
            "username-value",
            "password-value",
        ):
            self.assertNotIn(secret, rendered)

    def test_lease_maps_to_playwright_proxy(self):
        lease = ProxyLease(
            host="127.0.0.1",
            port=8080,
            username="proxy-user",
            password="proxy-password",
            expires_at=NOW + timedelta(seconds=420),
        )
        self.assertEqual(
            lease.playwright_proxy(),
            {
                "server": "http://127.0.0.1:8080",
                "username": "proxy-user",
                "password": "proxy-password",
            },
        )
        self.assertEqual(lease.remaining_ttl_seconds(NOW), 420)

    def test_whitelist_mode_omits_browser_proxy_credentials(self):
        config = ProxyConfig(
            enabled=True,
            secret_id="secret-id",
            signature="secret-token",
            proxy_auth_mode="whitelist",
        )
        config.validate()
        lease = ProxyLease(
            host="127.0.0.1",
            port=8080,
            username=None,
            password=None,
            expires_at=NOW + timedelta(seconds=420),
        )
        self.assertEqual(
            lease.playwright_proxy(),
            {"server": "http://127.0.0.1:8080"},
        )

    def test_unknown_proxy_auth_mode_is_rejected(self):
        config = ProxyConfig(
            enabled=True,
            secret_id="secret-id",
            signature="secret-token",
            proxy_auth_mode="unknown",
        )
        with self.assertRaisesRegex(ProxyConfigurationError, "KDL_PROXY_AUTH_MODE"):
            config.validate()


class KdlDpsProviderTests(unittest.TestCase):
    @staticmethod
    def _config(**overrides) -> ProxyConfig:
        values = {
            "enabled": True,
            "secret_id": "secret-id",
            "signature": "secret-token",
            "username": "proxy-user",
            "password": "proxy-password",
        }
        values.update(overrides)
        return ProxyConfig(**values)

    def test_token_request_and_relative_ttl_response(self):
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(dict(request.url.params))
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "0",
                    "data": {"proxy_list": ["192.0.2.10:18888,420"]},
                },
            )

        async def scenario() -> ProxyLease:
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            provider = KdlDpsProvider(
                self._config(),
                client=client,
                now=lambda: NOW,
            )
            try:
                return await provider.fetch()
            finally:
                await client.aclose()

        lease = asyncio.run(scenario())
        self.assertEqual(
            captured,
            {
                "secret_id": "secret-id",
                "sign_type": "token",
                "num": "1",
                "format": "json",
                "f_et": "1",
                "signature": "secret-token",
            },
        )
        self.assertEqual(lease.server, "http://192.0.2.10:18888")
        self.assertEqual(lease.expires_at, NOW + timedelta(seconds=420))

    def test_whitelist_mode_does_not_put_basic_credentials_in_lease(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"proxy_list": ["192.0.2.10:18888,420"]},
                },
            )

        async def scenario() -> ProxyLease:
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            provider = KdlDpsProvider(
                self._config(proxy_auth_mode="whitelist"),
                client=client,
                now=lambda: NOW,
            )
            try:
                return await provider.fetch()
            finally:
                await client.aclose()

        lease = asyncio.run(scenario())
        self.assertEqual(lease.playwright_proxy(), {"server": lease.server})

    def test_secret_key_switches_to_official_hmac_sha1_signature(self):
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(dict(request.url.params))
            return httpx.Response(
                200,
                json={"code": 0, "data": {"proxy_list": ["192.0.2.1:80,600"]}},
            )

        async def scenario() -> None:
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            provider = KdlDpsProvider(
                self._config(secret_key="hmac-key"),
                client=client,
                now=lambda: NOW,
            )
            try:
                await provider.fetch()
            finally:
                await client.aclose()

        asyncio.run(scenario())
        raw = (
            "GET/api/getdps?f_et=1&format=json&num=1&secret_id=secret-id"
            "&sign_type=hmacsha1&timestamp=1704067200"
        )
        expected = base64.b64encode(
            hmac.new(b"hmac-key", raw.encode(), hashlib.sha1).digest()
        ).decode()
        self.assertEqual(captured["sign_type"], "hmacsha1")
        self.assertEqual(captured["timestamp"], "1704067200")
        self.assertEqual(captured["signature"], expected)

    def test_provider_errors_do_not_include_credentials(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="unavailable")

        async def scenario() -> None:
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            provider = KdlDpsProvider(
                self._config(), client=client, now=lambda: NOW
            )
            try:
                with self.assertRaises(ProxyProviderError) as raised:
                    await provider.fetch()
                message = str(raised.exception)
                self.assertNotIn("secret-token", message)
                self.assertNotIn("proxy-password", message)
            finally:
                await client.aclose()

        asyncio.run(scenario())


class _FakeProvider:
    def __init__(self, leases: list[ProxyLease]) -> None:
        self.leases = leases
        self.calls = 0
        self.closed = False

    async def fetch(self) -> ProxyLease:
        self.calls += 1
        await asyncio.sleep(0)
        return self.leases.pop(0)

    async def aclose(self) -> None:
        self.closed = True


class DouyinProxyManagerTests(unittest.TestCase):
    @staticmethod
    def _config() -> ProxyConfig:
        return ProxyConfig(
            enabled=True,
            secret_id="secret-id",
            signature="secret-token",
            username="proxy-user",
            password="proxy-password",
        )

    @staticmethod
    def _lease(host: str, ttl: int) -> ProxyLease:
        return ProxyLease(
            host=host,
            port=8080,
            username="proxy-user",
            password="proxy-password",
            expires_at=NOW + timedelta(seconds=ttl),
        )

    def test_same_user_account_reuses_one_lease_under_concurrency(self):
        async def scenario() -> tuple[ProxyLease, ProxyLease, int]:
            provider = _FakeProvider([self._lease("192.0.2.1", 600)])
            manager = DouyinProxyManager(
                self._config(), provider=provider, now=lambda: NOW
            )
            first, second = await asyncio.gather(
                manager.acquire("user-a", "account-a", 300),
                manager.acquire("user-a", "account-a", 300),
            )
            return first, second, provider.calls

        first, second, calls = asyncio.run(scenario())
        self.assertIs(first, second)
        self.assertEqual(calls, 1)

    def test_ttl_threshold_rejects_short_new_lease(self):
        async def scenario() -> None:
            provider = _FakeProvider([self._lease("192.0.2.1", 299)])
            manager = DouyinProxyManager(
                self._config(), provider=provider, now=lambda: NOW
            )
            with self.assertRaises(ProxyTtlError):
                await manager.acquire("user-a", "account-a", 300)

        asyncio.run(scenario())

    def test_disabled_manager_does_not_call_provider(self):
        async def scenario() -> None:
            provider = _FakeProvider([])
            manager = DouyinProxyManager(
                ProxyConfig(enabled=False), provider=provider, now=lambda: NOW
            )
            with self.assertRaises(ProxyDisabledError):
                await manager.acquire("user-a", "account-a")
            self.assertEqual(provider.calls, 0)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
