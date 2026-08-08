from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.src.config import Settings
from app.src.domain.errors import ApiError
from app.src.persistence.repositories import Repository
from app.src.services.douyin_proxy import DouyinProxyManager
from app.src.services.douyin_proxy_policy import require_douyin_playwright_proxy
from sau_cli import (
    DOUYIN_PUBLISH_STRATEGY_IMMEDIATE,
    DOUYIN_PUBLISH_STRATEGY_SCHEDULED,
    TENCENT_PUBLISH_STRATEGY_IMMEDIATE,
    TENCENT_PUBLISH_STRATEGY_SCHEDULED,
    DouyinNoteUploadRequest,
    DouyinVideoUploadRequest,
    TencentVideoUploadRequest,
    upload_note,
    upload_tencent_video,
    upload_video,
)
from uploader.douyin_uploader.main import DouyinAuthenticationError


ProgressCallback = Callable[[str, str], Awaitable[None]]
VerificationProvider = Callable[[], Awaitable[str]]


def _platform_schedule(raw: str | None) -> datetime | int:
    if not raw:
        return 0
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return value.astimezone(ZoneInfo("Asia/Shanghai"))


class _BasePublisherService:
    def __init__(self, settings: Settings, repository: Repository) -> None:
        self.settings = settings
        self.repository = repository

    async def _material_path(self, user_id: str, material_id: str) -> Path:
        material = await self.repository.get_material_for_user(material_id, user_id)
        if material is None:
            raise RuntimeError(f"素材不存在或不属于用户 {user_id}: {material_id}")
        path = Path(material.stored_path)
        if not path.exists():
            raise RuntimeError(f"素材文件已丢失: {material_id}")
        return path


class DouyinPublisherService(_BasePublisherService):
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        proxy_manager: DouyinProxyManager | None = None,
    ) -> None:
        super().__init__(settings, repository)
        self.proxy_manager = proxy_manager

    async def _playwright_proxy(
        self,
        user_id: str,
        account: str,
        operation: str,
    ) -> dict[str, str]:
        return await require_douyin_playwright_proxy(
            self.proxy_manager,
            user_id,
            account,
            operation,
        )

    async def publish_video(
        self,
        user_id: str,
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
        video = await self._material_path(user_id, payload["video_material_id"])
        landscape = None
        portrait = None
        if payload.get("thumbnail_landscape_material_id"):
            landscape = await self._material_path(
                user_id, payload["thumbnail_landscape_material_id"]
            )
        if payload.get("thumbnail_portrait_material_id"):
            portrait = await self._material_path(
                user_id, payload["thumbnail_portrait_material_id"]
            )

        proxy = await self._playwright_proxy(user_id, account, "视频发布")

        temporary_cookie_path = payload.get("temporary_cookie_path")
        try:
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
                    account_file=(
                        self.settings.cookies_dir / user_id / f"douyin_{account}.json"
                    ),
                    initial_storage_state_file=(
                        Path(temporary_cookie_path) if temporary_cookie_path else None
                    ),
                    progress_callback=progress,
                    verification_code_provider=verification_provider,
                    publish_timeout_seconds=self.settings.video_timeout_seconds,
                    proxy=proxy,
                )
            )
        except DouyinAuthenticationError as exc:
            raise ApiError(
                409,
                "DOUYIN_COOKIE_INVALID",
                "抖音 Cookie 已失效或未形成可发布的登录态",
                {"browser_diagnostic": exc.diagnostic},
            ) from exc
        finally:
            if temporary_cookie_path:
                Path(temporary_cookie_path).unlink(missing_ok=True)
        account_path = self.settings.cookies_dir / user_id / f"douyin_{account}.json"
        await self.repository.upsert_account(
            user_id, "douyin", account, str(account_path), "valid"
        )
        return {"account": account, "platform": "douyin", "operation": "publish_video"}

    async def publish_note(
        self,
        user_id: str,
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
            await self._material_path(user_id, material_id)
            for material_id in payload["image_material_ids"]
        ]
        proxy = await self._playwright_proxy(user_id, account, "图文发布")
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
                account_file=self.settings.cookies_dir / user_id / f"douyin_{account}.json",
                progress_callback=progress,
                verification_code_provider=verification_provider,
                publish_timeout_seconds=self.settings.note_timeout_seconds,
                proxy=proxy,
            )
        )
        return {"account": account, "platform": "douyin", "operation": "publish_note"}


class ShipinPublisherService(_BasePublisherService):
    async def publish_video(
        self,
        user_id: str,
        account: str,
        payload: dict,
        progress: ProgressCallback,
    ) -> dict:
        schedule = _platform_schedule(payload.get("schedule"))
        strategy = (
            TENCENT_PUBLISH_STRATEGY_SCHEDULED
            if schedule != 0
            else TENCENT_PUBLISH_STRATEGY_IMMEDIATE
        )
        video = await self._material_path(user_id, payload["video_material_id"])
        landscape = None
        portrait = None
        if payload.get("thumbnail_landscape_material_id"):
            landscape = await self._material_path(
                user_id, payload["thumbnail_landscape_material_id"]
            )
        if payload.get("thumbnail_portrait_material_id"):
            portrait = await self._material_path(
                user_id, payload["thumbnail_portrait_material_id"]
            )
        await upload_tencent_video(
            TencentVideoUploadRequest(
                account_name=account,
                video_file=video,
                title=payload["title"],
                description=payload.get("description", ""),
                tags=payload.get("tags", []),
                publish_date=schedule,
                thumbnail_landscape_file=landscape,
                thumbnail_portrait_file=portrait,
                short_title=payload.get("short_title"),
                category=payload.get("category"),
                is_draft=False,
                publish_strategy=strategy,
                debug=self.settings.debug,
                headless=self.settings.shipin_headless,
                account_file=self.settings.cookies_dir / user_id / f"shipin_{account}.json",
                publish_timeout_seconds=self.settings.shipin_publish_timeout_seconds,
                progress_callback=progress,
            )
        )
        return {"account": account, "platform": "shipin", "operation": "publish_video"}


# Preserve the old import name for callers outside the API package.
PublisherService = DouyinPublisherService
