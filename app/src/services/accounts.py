from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

from app.src.config import Settings
from app.src.domain.errors import ApiError
from app.src.persistence.repositories import Repository
from sau_cli import convert_extension_cookies_to_storage_state
from uploader.douyin_uploader.main import cookie_auth as douyin_cookie_auth


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


class AccountService:
    def __init__(self, settings: Settings, repository: Repository) -> None:
        self.settings = settings
        self.repository = repository

    def cookie_path(self, account: str) -> Path:
        return self.settings.cookies_dir / f"douyin_{account}.json"

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

        path = self.settings.temporary_dir / f"{task_id}.cookie.json"
        path.write_text(json.dumps(storage_state, ensure_ascii=False), encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    async def execute_login(self, account: str, temporary_cookie_path: str) -> dict:
        temporary_path = Path(temporary_cookie_path)
        permanent_path = self.cookie_path(account)
        try:
            valid = await douyin_cookie_auth(str(temporary_path), headless=self.settings.headless)
            if not valid:
                # A failed replacement must not downgrade or overwrite a previously
                # persisted cookie. The explicit check endpoint can revalidate it.
                if not permanent_path.exists():
                    await self.repository.upsert_account(account, None, "invalid")
                raise RuntimeError("导入的抖音 cookie 已失效或缺少有效登录态")
            permanent_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary_path, permanent_path)
            os.chmod(permanent_path, 0o600)
            await self.repository.upsert_account(account, str(permanent_path), "valid")
            return {"account": account, "status": "valid"}
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    async def check(self, account: str) -> dict:
        path = self.cookie_path(account)
        if not path.exists():
            await self.repository.upsert_account(account, None, "missing")
            return {"account": account, "valid": False, "status": "missing"}
        try:
            valid = await asyncio.wait_for(
                douyin_cookie_auth(str(path), headless=self.settings.headless),
                timeout=self.settings.check_timeout_seconds,
            )
        except TimeoutError as exc:
            raise ApiError(504, "ACCOUNT_CHECK_TIMEOUT", "抖音账号检查超时") from exc
        status = "valid" if valid else "invalid"
        await self.repository.upsert_account(account, str(path), status)
        return {"account": account, "valid": valid, "status": status}
