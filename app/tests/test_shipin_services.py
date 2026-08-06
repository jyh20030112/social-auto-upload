from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.src.config import Settings
from app.src.persistence.database import Database
from app.src.persistence.repositories import Repository
from app.src.persistence.tables import MaterialRecord
from app.src.services.publisher import ShipinPublisherService


class ShipinPublisherServiceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        data_dir = Path(self.temporary.name)
        self.settings = Settings(
            data_dir=data_dir,
            database_url=f"sqlite+aiosqlite:///{data_dir / 'test.db'}",
            worker_enabled=False,
            shipin_headless=False,
        )
        self.settings.ensure_directories()
        self.database = Database(self.settings.database_url)
        await self.database.initialize()
        self.repository = Repository(self.database)

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

    async def test_video_request_reuses_cli_with_user_cookie_and_no_draft(self) -> None:
        video_path = await self._material("1" * 32, "video.mp4", "video")
        landscape_path = await self._material("2" * 32, "landscape.jpg", "image")
        progress = AsyncMock()
        with patch(
            "app.src.services.publisher.upload_tencent_video", new_callable=AsyncMock
        ) as upload:
            result = await ShipinPublisherService(
                self.settings, self.repository
            ).publish_video(
                "user_a",
                "channel_a",
                {
                    "video_material_id": "1" * 32,
                    "thumbnail_landscape_material_id": "2" * 32,
                    "thumbnail_portrait_material_id": None,
                    "title": "标题",
                    "description": "描述",
                    "tags": ["标签"],
                    "schedule": None,
                    "short_title": "视频号短标题",
                    "category": "科技",
                },
                progress,
            )

        request = upload.await_args.args[0]
        self.assertEqual(request.video_file, video_path)
        self.assertEqual(request.thumbnail_landscape_file, landscape_path)
        self.assertEqual(
            request.account_file,
            self.settings.cookies_dir / "user_a" / "shipin_channel_a.json",
        )
        self.assertFalse(request.is_draft)
        self.assertFalse(request.headless)
        self.assertEqual(request.publish_timeout_seconds, 120)
        self.assertIs(request.progress_callback, progress)
        self.assertEqual(result["platform"], "shipin")
        progress.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
