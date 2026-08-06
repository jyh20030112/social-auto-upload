from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from app.src.config import Settings
from app.src.domain.errors import ApiError
from app.src.domain.states import TERMINAL_TASK_STATUSES, TaskOperation, TaskStatus
from app.src.persistence.repositories import Repository
from app.src.persistence.tables import TaskRecord
from app.src.schemas.requests import LoginRequest, NotePublishRequest, VideoPublishRequest
from app.src.schemas.responses import iso_utc
from app.src.services.accounts import AccountService
from app.src.services.verification import VerificationHub

if TYPE_CHECKING:
    from app.src.services.task_worker import TaskWorker


def canonical_request_hash(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class TaskService:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        accounts: AccountService,
        verification: VerificationHub,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.accounts = accounts
        self.verification = verification
        self.worker: TaskWorker | None = None

    async def _idempotent_existing(
        self,
        account: str,
        operation: TaskOperation,
        idempotency_key: str | None,
        request_hash: str,
    ) -> TaskRecord | None:
        if not idempotency_key:
            return None
        existing = await self.repository.find_idempotent_task(
            account,
            operation.value,
            idempotency_key,
        )
        if existing is None:
            return None
        if existing.request_hash != request_hash:
            raise ApiError(
                409,
                "IDEMPOTENCY_CONFLICT",
                "相同 Idempotency-Key 已用于不同的请求体",
            )
        return existing

    async def submit_login(self, request: LoginRequest, idempotency_key: str | None = None) -> tuple[TaskRecord, bool]:
        hash_payload = {"account": request.account, "cookie": request.cookie}
        request_hash = canonical_request_hash(hash_payload)
        existing = await self._idempotent_existing(
            request.account,
            TaskOperation.LOGIN,
            idempotency_key,
            request_hash,
        )
        if existing is not None:
            return existing, True

        task_id = uuid4().hex
        temporary_path = self.accounts.prepare_login_cookie(task_id, request.cookie)
        try:
            task = await self.repository.create_task(
                task_id=task_id,
                account=request.account,
                operation=TaskOperation.LOGIN.value,
                payload={"temporary_cookie_path": str(temporary_path)},
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
        except IntegrityError:
            temporary_path.unlink(missing_ok=True)
            existing = await self._idempotent_existing(
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
        account: str,
        material_id: str,
        expected_kind: str,
    ) -> None:
        material = await self.repository.get_material_for_account(material_id, account)
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

    def _require_account_cookie(self, account: str) -> None:
        if not self.accounts.cookie_path(account).exists():
            raise ApiError(409, "ACCOUNT_NOT_LOGGED_IN", "账号尚未导入有效 Cookie")

    async def submit_video(
        self,
        request: VideoPublishRequest,
        idempotency_key: str,
    ) -> tuple[TaskRecord, bool]:
        self._require_account_cookie(request.account)
        payload = request.model_dump(mode="json")
        request_hash = canonical_request_hash(payload)
        existing = await self._idempotent_existing(
            request.account,
            TaskOperation.PUBLISH_VIDEO,
            idempotency_key,
            request_hash,
        )
        if existing is not None:
            return existing, True

        await self._validate_material(request.account, request.video_material_id, "video")
        material_ids = [request.video_material_id]
        for material_id in (
            request.thumbnail_landscape_material_id,
            request.thumbnail_portrait_material_id,
        ):
            if material_id:
                await self._validate_material(request.account, material_id, "image")
                material_ids.append(material_id)
        return await self._create_publish_task(
            request.account,
            TaskOperation.PUBLISH_VIDEO,
            payload,
            material_ids,
            idempotency_key,
            request_hash,
        )

    async def submit_note(
        self,
        request: NotePublishRequest,
        idempotency_key: str,
    ) -> tuple[TaskRecord, bool]:
        self._require_account_cookie(request.account)
        payload = request.model_dump(mode="json")
        request_hash = canonical_request_hash(payload)
        existing = await self._idempotent_existing(
            request.account,
            TaskOperation.PUBLISH_NOTE,
            idempotency_key,
            request_hash,
        )
        if existing is not None:
            return existing, True
        for material_id in request.image_material_ids:
            await self._validate_material(request.account, material_id, "image")
        return await self._create_publish_task(
            request.account,
            TaskOperation.PUBLISH_NOTE,
            payload,
            request.image_material_ids,
            idempotency_key,
            request_hash,
        )

    async def _create_publish_task(
        self,
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
                account,
                operation,
                idempotency_key,
                request_hash,
            )
            if existing is None:
                raise
            return existing, True

    async def get_for_account(self, task_id: str, account: str) -> TaskRecord:
        task = await self.repository.get_task(task_id, account)
        if task is None:
            raise ApiError(404, "TASK_NOT_FOUND", "任务不存在")
        return task

    async def serialize(self, task: TaskRecord, include_events: bool = False) -> dict:
        data = {
            "id": task.id,
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
        return data

    async def submit_verification_code(self, task_id: str, account: str, code: str) -> None:
        task = await self.get_for_account(task_id, account)
        if task.status != TaskStatus.WAITING_VERIFICATION.value:
            raise ApiError(
                409,
                "TASK_NOT_WAITING_VERIFICATION",
                "任务当前没有等待短信验证码",
            )
        if not await self.verification.submit(task_id, code):
            raise ApiError(409, "VERIFICATION_CODE_ALREADY_SUBMITTED", "验证码已经提交")

    async def cancel(self, task_id: str, account: str) -> TaskRecord:
        task = await self.get_for_account(task_id, account)
        if TaskStatus(task.status) in TERMINAL_TASK_STATUSES:
            return task
        updated = await self.repository.request_task_cancel(task_id, account)
        if updated is None:
            raise ApiError(404, "TASK_NOT_FOUND", "任务不存在")
        if (
            updated.status == TaskStatus.CANCELLED.value
            and updated.operation == TaskOperation.LOGIN.value
        ):
            Path(updated.payload["temporary_cookie_path"]).unlink(missing_ok=True)
        await self.verification.cancel(task_id)
        if self.worker is not None:
            self.worker.cancel_running(task_id)
        return updated
