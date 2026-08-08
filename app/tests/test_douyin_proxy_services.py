from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.src.config import Settings
from app.src.domain.errors import ApiError
from app.src.persistence.database import Database
from app.src.persistence.repositories import Repository
from app.src.persistence.tables import MaterialRecord
from app.src.services.accounts import DouyinAccountService
from app.src.services.browser_coordinator import BrowserCoordinator
from app.src.services.publisher import DouyinPublisherService


PROXY = {
    "server": "http://proxy.example:12345",
    "username": "proxy-user",
    "password": "proxy-password",
}


class _FakeEndpoint:
    def playwright_proxy(self) -> dict[str, str]:
        return dict(PROXY)


class _FakeProxyManager:
    enabled = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def acquire(
        self,
        user_id: str,
        account: str,
    ) -> _FakeEndpoint:
        self.calls.append((user_id, account))
        return _FakeEndpoint()


class DouyinProxyServiceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        data_dir = Path(self.temporary.name)
        self.settings = Settings(
            data_dir=data_dir,
            database_url=f"sqlite+aiosqlite:///{data_dir / 'test.db'}",
            worker_enabled=False,
        )
        self.settings.ensure_directories()
        self.database = Database(self.settings.database_url)
        await self.database.initialize()
        self.repository = Repository(self.database)
        self.proxy_manager = _FakeProxyManager()

    async def asyncTearDown(self) -> None:
        await self.database.close()
        self.temporary.cleanup()

    async def _material(self, material_id: str, name: str, kind: str) -> Path:
        path = self.settings.materials_dir / "user_a" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"content")
        await self.repository.add_material(
            MaterialRecord(
                id=material_id,
                user_id="user_a",
                original_name=name,
                stored_path=str(path),
                kind=kind,
                extension=path.suffix,
                mime_type=None,
                size_bytes=7,
                sha256=material_id * 2,
            )
        )
        return path

    async def test_account_check_uses_account_tunnel_endpoint(self) -> None:
        service = DouyinAccountService(
            self.settings,
            self.repository,
            BrowserCoordinator(1),
            self.proxy_manager,
        )
        cookie_path = service.cookie_path("user_a", "creator")
        cookie_path.parent.mkdir(parents=True, exist_ok=True)
        cookie_path.write_text("{}", encoding="utf-8")

        with patch(
            "app.src.services.accounts.douyin_cookie_auth",
            new_callable=AsyncMock,
            return_value=True,
        ) as cookie_auth:
            result = await service.check("user_a", "creator")

        self.assertTrue(result["valid"])
        self.assertEqual(
            self.proxy_manager.calls,
            [("user_a", "creator")],
        )
        self.assertEqual(cookie_auth.await_args.kwargs["proxy"], PROXY)

    async def test_account_login_uses_account_tunnel_endpoint(self) -> None:
        service = DouyinAccountService(
            self.settings,
            self.repository,
            BrowserCoordinator(1),
            self.proxy_manager,
        )
        temporary_path = self.settings.temporary_dir / "login-cookie.json"
        temporary_path.write_text("{}", encoding="utf-8")

        with patch(
            "app.src.services.accounts.douyin_cookie_auth",
            new_callable=AsyncMock,
            return_value=True,
        ) as cookie_auth:
            result = await service.execute_login(
                "user_a",
                "creator",
                str(temporary_path),
            )

        self.assertEqual(result["status"], "valid")
        self.assertEqual(
            self.proxy_manager.calls,
            [("user_a", "creator")],
        )
        self.assertEqual(cookie_auth.await_args.kwargs["proxy"], PROXY)

    async def test_account_login_returns_browser_diagnostic_when_cookie_is_invalid(
        self,
    ) -> None:
        service = DouyinAccountService(
            self.settings,
            self.repository,
            BrowserCoordinator(1),
            self.proxy_manager,
        )
        temporary_path = self.settings.temporary_dir / "invalid-cookie.json"
        temporary_path.write_text("{}", encoding="utf-8")
        diagnostic = {
            "attempt": 3,
            "reason": "login_required",
            "final_url": "https://creator.douyin.com/creator-micro/content/upload",
            "login_markers": ["phone_input"],
            "upload_input_count": 0,
            "visible_text": "手机号登录 请输入手机号",
        }

        async def invalid_cookie_auth(*_args, **kwargs):
            kwargs["diagnostic_callback"](diagnostic)
            return False

        with patch(
            "app.src.services.accounts.douyin_cookie_auth",
            side_effect=invalid_cookie_auth,
        ):
            with self.assertRaises(ApiError) as raised:
                await service.execute_login(
                    "user_a",
                    "creator",
                    str(temporary_path),
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.code, "DOUYIN_COOKIE_INVALID")
        self.assertEqual(
            raised.exception.details["browser_diagnostic"],
            diagnostic,
        )

    async def test_video_publish_passes_tunnel_proxy(self) -> None:
        video_path = await self._material("1" * 32, "video.mp4", "video")
        service = DouyinPublisherService(
            self.settings,
            self.repository,
            self.proxy_manager,
        )

        with patch(
            "app.src.services.publisher.upload_video", new_callable=AsyncMock
        ) as upload:
            await service.publish_video(
                "user_a",
                "creator",
                {
                    "video_material_id": "1" * 32,
                    "title": "标题",
                    "description": "描述",
                    "tags": [],
                },
                AsyncMock(),
                AsyncMock(),
            )

        request = upload.await_args.args[0]
        self.assertEqual(request.video_file, video_path)
        self.assertEqual(request.proxy, PROXY)
        self.assertEqual(
            self.proxy_manager.calls,
            [("user_a", "creator")],
        )

    async def test_note_publish_passes_tunnel_proxy(self) -> None:
        image_path = await self._material("2" * 32, "image.jpg", "image")
        service = DouyinPublisherService(
            self.settings,
            self.repository,
            self.proxy_manager,
        )

        with patch(
            "app.src.services.publisher.upload_note", new_callable=AsyncMock
        ) as upload:
            await service.publish_note(
                "user_a",
                "creator",
                {
                    "image_material_ids": ["2" * 32],
                    "title": "标题",
                    "note": "正文",
                    "tags": [],
                },
                AsyncMock(),
                AsyncMock(),
            )

        request = upload.await_args.args[0]
        self.assertEqual(request.image_files, [image_path])
        self.assertEqual(request.proxy, PROXY)
        self.assertEqual(
            self.proxy_manager.calls,
            [("user_a", "creator")],
        )


if __name__ == "__main__":
    unittest.main()
