from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.src.config import Settings
from app.src.persistence.repositories import Repository
from sau_cli import (
    DOUYIN_PUBLISH_STRATEGY_IMMEDIATE,
    DOUYIN_PUBLISH_STRATEGY_SCHEDULED,
    DouyinNoteUploadRequest,
    DouyinVideoUploadRequest,
    upload_note,
    upload_video,
)


ProgressCallback = Callable[[str, str], Awaitable[None]]
VerificationProvider = Callable[[], Awaitable[str]]


def _platform_schedule(raw: str | None) -> datetime | int:
    if not raw:
        return 0
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return value.astimezone(ZoneInfo("Asia/Shanghai"))


class PublisherService:
    def __init__(self, settings: Settings, repository: Repository) -> None:
        self.settings = settings
        self.repository = repository

    async def _material_path(self, account: str, material_id: str) -> Path:
        material = await self.repository.get_material_for_account(material_id, account)
        if material is None:
            raise RuntimeError(f"素材不存在或不属于账号 {account}: {material_id}")
        path = Path(material.stored_path)
        if not path.exists():
            raise RuntimeError(f"素材文件已丢失: {material_id}")
        return path

    async def publish_video(
        self,
        account: str,
        payload: dict,
        progress: ProgressCallback,
        verification_provider: VerificationProvider,
    ) -> dict:
        schedule = _platform_schedule(payload.get("schedule"))
        strategy = (
            DOUYIN_PUBLISH_STRATEGY_SCHEDULED
            if schedule != 0
            else DOUYIN_PUBLISH_STRATEGY_IMMEDIATE
        )
        video = await self._material_path(account, payload["video_material_id"])
        landscape = None
        portrait = None
        if payload.get("thumbnail_landscape_material_id"):
            landscape = await self._material_path(account, payload["thumbnail_landscape_material_id"])
        if payload.get("thumbnail_portrait_material_id"):
            portrait = await self._material_path(account, payload["thumbnail_portrait_material_id"])

        await upload_video(
            DouyinVideoUploadRequest(
                account_name=account,
                video_file=video,
                title=payload["title"],
                description=payload.get("description", ""),
                tags=payload.get("tags", []),
                publish_date=schedule,
                thumbnail_landscape_file=landscape,
                thumbnail_portrait_file=portrait,
                product_link=payload.get("product_link", ""),
                product_title=payload.get("product_title", ""),
                declaration=payload.get("declaration"),
                publish_strategy=strategy,
                debug=self.settings.debug,
                headless=self.settings.headless,
                account_file=self.settings.cookies_dir / f"douyin_{account}.json",
                progress_callback=progress,
                verification_code_provider=verification_provider,
                publish_timeout_seconds=self.settings.video_timeout_seconds,
            )
        )
        return {"account": account, "operation": "publish_video"}

    async def publish_note(
        self,
        account: str,
        payload: dict,
        progress: ProgressCallback,
        verification_provider: VerificationProvider,
    ) -> dict:
        schedule = _platform_schedule(payload.get("schedule"))
        strategy = (
            DOUYIN_PUBLISH_STRATEGY_SCHEDULED
            if schedule != 0
            else DOUYIN_PUBLISH_STRATEGY_IMMEDIATE
        )
        images = [
            await self._material_path(account, material_id)
            for material_id in payload["image_material_ids"]
        ]
        await upload_note(
            DouyinNoteUploadRequest(
                account_name=account,
                image_files=images,
                title=payload["title"],
                note=payload.get("note", ""),
                tags=payload.get("tags", []),
                publish_date=schedule,
                publish_strategy=strategy,
                debug=self.settings.debug,
                headless=self.settings.headless,
                bgm=payload.get("bgm", ""),
                account_file=self.settings.cookies_dir / f"douyin_{account}.json",
                progress_callback=progress,
                verification_code_provider=verification_provider,
                publish_timeout_seconds=self.settings.note_timeout_seconds,
            )
        )
        return {"account": account, "operation": "publish_note"}
