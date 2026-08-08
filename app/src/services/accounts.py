from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

from app.src.config import Settings
from app.src.domain.errors import ApiError
from app.src.domain.states import Platform
from app.src.persistence.repositories import Repository
from app.src.services.browser_coordinator import BrowserCoordinator
from app.src.services.douyin_proxy import DouyinProxyManager
from sau_cli import (
    convert_extension_cookies_to_storage_state,
    convert_tencent_cookie_header_to_storage_state,
)
from uploader.douyin_uploader.main import cookie_auth as douyin_cookie_auth
from uploader.tencent_uploader.main import cookie_auth as tencent_cookie_auth


COOKIE_NAME_PATTERN = re.compile(r"^[^\s;=]+$")


def parse_raw_cookie_header(cookie_text: str) -> list[dict]:
    """将浏览器请求头形式的 Cookie 转为 Cookie-Editor 兼容结构。"""
    raw = cookie_text.strip()
    if raw[:7].lower() == "cookie:":
        raw = raw[7:].strip()
    if not raw or "\r" in raw or "\n" in raw:
        raise ValueError("Cookie 请求头为空或包含非法换行")

    values: dict[str, str] = {}
    for segment in raw.split(";"):
        segment = segment.strip()
        if not segment:
            continue
        if "=" not in segment:
            raise ValueError(f"Cookie 片段缺少等号: {segment[:32]}")
        name, value = segment.split("=", 1)
        name = name.strip()
        if not COOKIE_NAME_PATTERN.fullmatch(name):
            raise ValueError(f"Cookie 名称无效: {name[:32]}")
        values[name] = value.strip()
        if len(values) > 300:
            raise ValueError("Cookie 数量超过 300 个")

    if not values:
        raise ValueError("Cookie 请求头中没有有效的 name=value 字段")
    return [
        {
            "name": name,
            "value": value,
            "domain": ".douyin.com",
            "path": "/",
            "secure": True,
            "httpOnly": False,
            "sameSite": "Lax",
        }
        for name, value in values.items()
    ]


class DouyinAccountService:
    platform = Platform.DOUYIN.value

    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        coordinator: BrowserCoordinator,
        proxy_manager: DouyinProxyManager | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.coordinator = coordinator
        self.proxy_manager = proxy_manager

    async def _playwright_proxy(
        self,
        user_id: str,
        account: str,
    ) -> dict[str, str] | None:
        if self.proxy_manager is None or not self.proxy_manager.enabled:
            return None
        endpoint = await self.proxy_manager.acquire(user_id, account)
        return endpoint.playwright_proxy()

    def cookie_path(self, user_id: str, account: str) -> Path:
        return self.settings.cookies_dir / user_id / f"douyin_{account}.json"

    def prepare_login_cookie(self, task_id: str, cookie_text: str) -> Path:
        stripped = cookie_text.strip()
        if stripped.startswith(("[", "{")):
            try:
                raw = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ApiError(
                    422,
                    "INVALID_COOKIE_JSON",
                    "cookie 不是有效的 JSON 字符串",
                    {"reason": str(exc)},
                ) from exc
        else:
            try:
                raw = parse_raw_cookie_header(stripped)
            except ValueError as exc:
                raise ApiError(422, "INVALID_COOKIE", str(exc)) from exc
        try:
            storage_state = convert_extension_cookies_to_storage_state(raw)
        except ValueError as exc:
            raise ApiError(422, "INVALID_COOKIE", str(exc)) from exc
        return _write_temporary_cookie(self.settings, task_id, storage_state)

    async def execute_login(
        self,
        user_id: str,
        account: str,
        temporary_cookie_path: str,
    ) -> dict:
        temporary_path = Path(temporary_cookie_path)
        permanent_path = self.cookie_path(user_id, account)
        browser_diagnostics: list[dict] = []
        screenshot_path = (
            self.settings.temporary_dir
            / "diagnostics"
            / f"{temporary_path.stem}.png"
            if self.settings.debug
            else None
        )
        try:
            proxy = await self._playwright_proxy(user_id, account)
            valid = await douyin_cookie_auth(
                str(temporary_path),
                headless=self.settings.headless,
                proxy=proxy,
                diagnostic_callback=browser_diagnostics.append,
                diagnostic_screenshot_path=screenshot_path,
            )
            if not valid:
                if not permanent_path.exists():
                    await self.repository.upsert_account(
                        user_id, self.platform, account, None, "invalid"
                    )
                raise ApiError(
                    409,
                    "DOUYIN_COOKIE_INVALID",
                    "导入的抖音 cookie 已失效或缺少有效登录态",
                    {
                        "browser_diagnostic": (
                            browser_diagnostics[-1]
                            if browser_diagnostics
                            else {"reason": "no_browser_diagnostic"}
                        )
                    },
                )
            permanent_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary_path, permanent_path)
            os.chmod(permanent_path, 0o600)
            await self.repository.upsert_account(
                user_id, self.platform, account, str(permanent_path), "valid"
            )
            return {"account": account, "platform": self.platform, "status": "valid"}
        finally:
            temporary_path.unlink(missing_ok=True)

    async def check(self, user_id: str, account: str) -> dict:
        async def run() -> dict:
            async with self.coordinator.slot(user_id, self.platform, account):
                path = self.cookie_path(user_id, account)
                if not path.exists():
                    await self.repository.upsert_account(
                        user_id, self.platform, account, None, "missing"
                    )
                    return {
                        "account": account,
                        "platform": self.platform,
                        "valid": False,
                        "status": "missing",
                    }
                proxy = await self._playwright_proxy(user_id, account)
                valid = await douyin_cookie_auth(
                    str(path),
                    headless=self.settings.headless,
                    proxy=proxy,
                )
                status = "valid" if valid else "invalid"
                await self.repository.upsert_account(
                    user_id, self.platform, account, str(path), status
                )
                return {
                    "account": account,
                    "platform": self.platform,
                    "valid": valid,
                    "status": status,
                }

        try:
            return await asyncio.wait_for(run(), timeout=self.settings.check_timeout_seconds)
        except TimeoutError as exc:
            raise ApiError(504, "ACCOUNT_CHECK_TIMEOUT", "抖音账号检查超时") from exc


class ShipinAccountService:
    platform = Platform.SHIPIN.value

    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        coordinator: BrowserCoordinator,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.coordinator = coordinator

    def cookie_path(self, user_id: str, account: str) -> Path:
        return self.settings.cookies_dir / user_id / f"shipin_{account}.json"

    def prepare_login_cookie(self, task_id: str, cookie_text: str) -> Path:
        try:
            storage_state = convert_tencent_cookie_header_to_storage_state(cookie_text)
        except ValueError as exc:
            raise ApiError(422, "INVALID_COOKIE", str(exc)) from exc
        return _write_temporary_cookie(self.settings, task_id, storage_state)

    async def execute_login(
        self,
        user_id: str,
        account: str,
        temporary_cookie_path: str,
    ) -> dict:
        temporary_path = Path(temporary_cookie_path)
        permanent_path = self.cookie_path(user_id, account)
        try:
            valid = await tencent_cookie_auth(
                str(temporary_path), headless=self.settings.shipin_headless
            )
            if not valid:
                if not permanent_path.exists():
                    await self.repository.upsert_account(
                        user_id, self.platform, account, None, "invalid"
                    )
                raise RuntimeError("导入的视频号 Cookie 校验失败，请确认 wxuin 和 sessionid 有效")
            permanent_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary_path, permanent_path)
            os.chmod(permanent_path, 0o600)
            await self.repository.upsert_account(
                user_id, self.platform, account, str(permanent_path), "valid"
            )
            return {"account": account, "platform": self.platform, "status": "valid"}
        finally:
            temporary_path.unlink(missing_ok=True)

    async def check(self, user_id: str, account: str) -> dict:
        async def run() -> dict:
            async with self.coordinator.slot(user_id, self.platform, account):
                path = self.cookie_path(user_id, account)
                if not path.exists():
                    await self.repository.upsert_account(
                        user_id, self.platform, account, None, "missing"
                    )
                    return {
                        "account": account,
                        "platform": self.platform,
                        "valid": False,
                        "status": "missing",
                    }
                valid = await tencent_cookie_auth(
                    str(path), headless=self.settings.shipin_headless
                )
                status = "valid" if valid else "invalid"
                await self.repository.upsert_account(
                    user_id, self.platform, account, str(path), status
                )
                return {
                    "account": account,
                    "platform": self.platform,
                    "valid": valid,
                    "status": status,
                }

        try:
            return await asyncio.wait_for(
                run(), timeout=self.settings.shipin_check_timeout_seconds
            )
        except TimeoutError as exc:
            raise ApiError(504, "ACCOUNT_CHECK_TIMEOUT", "视频号账号检查超时") from exc


def _write_temporary_cookie(settings: Settings, task_id: str, storage_state: dict) -> Path:
    path = settings.temporary_dir / f"{task_id}.cookie.json"
    path.write_text(json.dumps(storage_state, ensure_ascii=False), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


# Preserve the old import name for callers outside the API package.
AccountService = DouyinAccountService
