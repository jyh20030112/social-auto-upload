from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from app.src.config import Settings
from app.src.domain.errors import ApiError
from app.src.domain.states import Platform, TERMINAL_TASK_STATUSES, TaskOperation, TaskStatus
from app.src.persistence.repositories import Repository
from app.src.persistence.tables import TaskRecord
from app.src.schemas.requests import (
    LoginRequest,
    NotePublishRequest,
    ShipinLoginRequest,
    ShipinVideoPublishRequest,
    VideoPublishRequest,
)
from app.src.schemas.responses import iso_utc
from app.src.services.accounts import DouyinAccountService, ShipinAccountService
from app.src.services.verification import VerificationHub

if TYPE_CHECKING:
    from app.src.services.material_worker import MaterialWorker
    from app.src.services.task_worker import TaskWorker


def canonical_request_hash(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class TaskService:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        douyin_accounts: DouyinAccountService,
        shipin_accounts: ShipinAccountService,
        verification: VerificationHub,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.douyin_accounts = douyin_accounts
        self.shipin_accounts = shipin_accounts
        self.verification = verification
        self.worker: TaskWorker | None = None
        self.material_worker: MaterialWorker | None = None

    def _accounts(self, platform: str):
        if platform == Platform.DOUYIN.value:
            return self.douyin_accounts
        if platform == Platform.SHIPIN.value:
            return self.shipin_accounts
        raise ValueError(f"不支持的平台: {platform}")

    async def _idempotent_existing(
        self,
        user_id: str,
        platform: str,
        account: str,
        operation: TaskOperation,
        idempotency_key: str | None,
        request_hash: str,
    ) -> TaskRecord | None:
        if not idempotency_key:
            return None
        existing = await self.repository.find_idempotent_task(
            user_id,
            platform,
            account,
            operation.value,
            idempotency_key,
        )
        if existing is None:
            return None
        if existing.request_hash != request_hash:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "相同 Idempotency-Key 已用于不同的请求体")
        return existing

    async def submit_login(
        self,
        user_id: str,
        platform: Platform,
        request: LoginRequest | ShipinLoginRequest,
        idempotency_key: str | None = None,
    ) -> tuple[TaskRecord, bool]:
        callback_url = str(request.callback_url) if request.callback_url else None
        hash_payload = {
            "account": request.account,
            "cookie": request.cookie,
            "callback_url": callback_url,
        }
        request_hash = canonical_request_hash(hash_payload)
        existing = await self._idempotent_existing(
            user_id,
            platform.value,
            request.account,
            TaskOperation.LOGIN,
            idempotency_key,
            request_hash,
        )
        if existing is not None:
            return existing, True

        task_id = uuid4().hex
        temporary_path = self._accounts(platform.value).prepare_login_cookie(task_id, request.cookie)
        try:
            task = await self.repository.create_task(
                task_id=task_id,
                user_id=user_id,
                platform=platform.value,
                account=request.account,
                operation=TaskOperation.LOGIN.value,
                payload={
                    "temporary_cookie_path": str(temporary_path),
                    "callback_url": callback_url,
                },
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
        except IntegrityError:
            temporary_path.unlink(missing_ok=True)
            existing = await self._idempotent_existing(
                user_id,
                platform.value,
                request.account,
                TaskOperation.LOGIN,
                idempotency_key,
                request_hash,
            )
            if existing is None:
                raise
            return existing, True
        return task, False

    async def _validate_material(
        self,
        user_id: str,
        material_id: str,
        expected_kind: str,
    ) -> None:
        material = await self.repository.get_material_for_user(material_id, user_id)
        if material is None:
            raise ApiError(404, "MATERIAL_NOT_FOUND", f"素材不存在: {material_id}")
        if material.kind != expected_kind:
            raise ApiError(
                422,
                "MATERIAL_TYPE_MISMATCH",
                f"素材 {material_id} 必须是 {expected_kind} 类型",
            )
        if not Path(material.stored_path).exists():
            raise ApiError(409, "MATERIAL_FILE_MISSING", f"素材文件已丢失: {material_id}")

    def _require_account_cookie(self, user_id: str, platform: str, account: str) -> None:
        if not self._accounts(platform).cookie_path(user_id, account).exists():
            raise ApiError(409, "ACCOUNT_NOT_LOGGED_IN", "账号尚未导入有效 Cookie")

    async def submit_video(
        self,
        user_id: str,
        platform: Platform,
        request: VideoPublishRequest | ShipinVideoPublishRequest,
        idempotency_key: str,
    ) -> tuple[TaskRecord, bool]:
        self._require_account_cookie(user_id, platform.value, request.account)
        payload = request.model_dump(mode="json")
        request_hash = canonical_request_hash(payload)
        existing = await self._idempotent_existing(
            user_id,
            platform.value,
            request.account,
            TaskOperation.PUBLISH_VIDEO,
            idempotency_key,
            request_hash,
        )
        if existing is not None:
            return existing, True

        await self._validate_material(user_id, request.video_material_id, "video")
        material_ids = [request.video_material_id]
        for material_id in (
            request.thumbnail_landscape_material_id,
            request.thumbnail_portrait_material_id,
        ):
            if material_id:
                await self._validate_material(user_id, material_id, "image")
                material_ids.append(material_id)
        return await self._create_publish_task(
            user_id,
            platform.value,
            request.account,
            TaskOperation.PUBLISH_VIDEO,
            payload,
            material_ids,
            idempotency_key,
            request_hash,
        )

    async def submit_note(
        self,
        user_id: str,
        request: NotePublishRequest,
        idempotency_key: str,
    ) -> tuple[TaskRecord, bool]:
        platform = Platform.DOUYIN.value
        self._require_account_cookie(user_id, platform, request.account)
        payload = request.model_dump(mode="json")
        request_hash = canonical_request_hash(payload)
        existing = await self._idempotent_existing(
            user_id,
            platform,
            request.account,
            TaskOperation.PUBLISH_NOTE,
            idempotency_key,
            request_hash,
        )
        if existing is not None:
            return existing, True
        for material_id in request.image_material_ids:
            await self._validate_material(user_id, material_id, "image")
        return await self._create_publish_task(
            user_id,
            platform,
            request.account,
            TaskOperation.PUBLISH_NOTE,
            payload,
            request.image_material_ids,
            idempotency_key,
            request_hash,
        )

    async def _create_publish_task(
        self,
        user_id: str,
        platform: str,
        account: str,
        operation: TaskOperation,
        payload: dict,
        material_ids: list[str],
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[TaskRecord, bool]:
        try:
            task = await self.repository.create_task(
                task_id=uuid4().hex,
                user_id=user_id,
                platform=platform,
                account=account,
                operation=operation.value,
                payload=payload,
                material_ids=material_ids,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            return task, False
        except IntegrityError:
            existing = await self._idempotent_existing(
                user_id,
                platform,
                account,
                operation,
                idempotency_key,
                request_hash,
            )
            if existing is None:
                raise
            return existing, True

    async def get_for_user(self, task_id: str, user_id: str) -> TaskRecord:
        task = await self.repository.get_task(task_id, user_id)
        if task is None:
            raise ApiError(404, "TASK_NOT_FOUND", "任务不存在")
        return task

    async def serialize(self, task: TaskRecord, include_events: bool = False) -> dict:
        data = {
            "id": task.id,
            "user_id": task.user_id,
            "platform": task.platform,
            "account": task.account,
            "operation": task.operation,
            "status": task.status,
            "stage": task.stage,
            "result": task.result,
            "error": (
                {
                    "code": task.error_code,
                    "message": task.error_message,
                    "details": task.error_details or {},
                }
                if task.error_code or task.error_message
                else None
            ),
            "created_at": iso_utc(task.created_at),
            "started_at": iso_utc(task.started_at),
            "finished_at": iso_utc(task.finished_at),
        }
        if include_events:
            events = await self.repository.list_task_events(task.id, limit=50)
            data["events"] = [
                {
                    "id": event.id,
                    "stage": event.stage,
                    "message": event.message,
                    "created_at": iso_utc(event.created_at),
                }
                for event in events
            ]
        if task.payload.get("callback_url"):
            callbacks = await self.repository.list_task_callbacks(task.id)
            data["callbacks"] = [
                {
                    "event_id": callback.id,
                    "event": callback.event_type,
                    "status": callback.status,
                    "attempts": callback.attempts,
                    "last_error": callback.last_error,
                    "delivered_at": iso_utc(callback.delivered_at),
                }
                for callback in callbacks
            ]
        return data

    def _operation_timeout(self, task: TaskRecord) -> int:
        if task.platform == Platform.SHIPIN.value:
            if task.operation == TaskOperation.LOGIN.value:
                return self.settings.shipin_login_timeout_seconds
            return self.settings.shipin_video_timeout_seconds
        if task.operation == TaskOperation.LOGIN.value:
            return self.settings.login_timeout_seconds
        if task.operation == TaskOperation.PUBLISH_VIDEO.value:
            return self.settings.video_timeout_seconds
        if task.operation == TaskOperation.PUBLISH_NOTE.value:
            return self.settings.note_timeout_seconds
        return self.settings.material_timeout_seconds

    async def wait_for_result(self, task: TaskRecord) -> tuple[dict, int]:
        if not self.settings.worker_enabled:
            raise ApiError(503, "WORKER_DISABLED", "任务执行器未启用")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._operation_timeout(task) + 5
        current = task
        while True:
            status = TaskStatus(current.status)
            if status == TaskStatus.WAITING_VERIFICATION:
                events = await self.repository.list_task_events(current.id, limit=50)
                waiting_event = next(
                    (event for event in reversed(events) if event.stage == "waiting_verification"),
                    None,
                )
                expires_at = (
                    waiting_event.created_at
                    + timedelta(seconds=self.settings.verification_timeout_seconds)
                    if waiting_event is not None
                    else None
                )
                return {
                    "task_id": current.id,
                    "status": current.status,
                    "stage": current.stage,
                    "verification_expires_at": iso_utc(expires_at),
                }, 202
            if status in TERMINAL_TASK_STATUSES:
                if status == TaskStatus.SUCCEEDED:
                    return current.result or {}, 200
                if status == TaskStatus.CANCELLED:
                    raise ApiError(
                        409,
                        current.error_code or "TASK_CANCELLED",
                        current.error_message or "任务已取消",
                    )
                if status == TaskStatus.INTERRUPTED:
                    raise ApiError(
                        409,
                        current.error_code or "TASK_INTERRUPTED",
                        current.error_message or "任务执行被中断，平台端结果可能未知",
                        current.error_details or {},
                    )
                details = dict(current.error_details or {})
                details.setdefault("task_id", current.id)
                if current.error_code == "TASK_TIMEOUT":
                    status_code = 504
                elif current.error_code == "DOUYIN_COOKIE_INVALID":
                    status_code = 409
                elif current.error_code == "DOUYIN_PROXY_REQUIRED":
                    status_code = 503
                elif current.error_code == "DOUYIN_PROXY_UNAVAILABLE":
                    status_code = 502
                else:
                    status_code = 500
                raise ApiError(
                    status_code,
                    current.error_code or "TASK_FAILED",
                    current.error_message or "任务执行失败",
                    details,
                )
            if loop.time() >= deadline:
                raise ApiError(504, "TASK_WAIT_TIMEOUT", "等待任务结果超时，任务可能仍在后台执行")
            await asyncio.sleep(0.2)
            refreshed = await self.repository.get_task(current.id, current.user_id)
            if refreshed is None:
                raise ApiError(404, "TASK_NOT_FOUND", "任务不存在")
            current = refreshed

    async def submit_verification_code(self, task_id: str, user_id: str, code: str) -> None:
        task = await self.get_for_user(task_id, user_id)
        if task.platform != Platform.DOUYIN.value:
            raise ApiError(404, "TASK_NOT_FOUND", "任务不存在")
        if task.status != TaskStatus.WAITING_VERIFICATION.value:
            raise ApiError(409, "TASK_NOT_WAITING_VERIFICATION", "任务当前没有等待短信验证码")
        if not await self.verification.submit(task_id, code):
            raise ApiError(409, "VERIFICATION_CODE_ALREADY_SUBMITTED", "验证码已经提交")

    async def cancel(self, task_id: str, user_id: str) -> TaskRecord:
        task = await self.get_for_user(task_id, user_id)
        if TaskStatus(task.status) in TERMINAL_TASK_STATUSES:
            return task
        updated = await self.repository.request_task_cancel(task_id, user_id)
        if updated is None:
            raise ApiError(404, "TASK_NOT_FOUND", "任务不存在")
        if updated.status == TaskStatus.CANCELLED.value and updated.operation == TaskOperation.LOGIN.value:
            Path(updated.payload["temporary_cookie_path"]).unlink(missing_ok=True)
        if (
            updated.status == TaskStatus.CANCELLED.value
            and updated.operation == TaskOperation.UPLOAD_MATERIALS.value
        ):
            from app.src.services.materials import MaterialService

            MaterialService.cleanup_staged(updated.payload)
        await self.verification.cancel(task_id)
        if self.worker is not None:
            self.worker.cancel_running(task_id)
        if self.material_worker is not None:
            self.material_worker.cancel_running(task_id)
        return updated
