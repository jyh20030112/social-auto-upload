from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import delete, exists, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from app.src.domain.states import ACTIVE_TASK_STATUSES, TaskStage, TaskStatus
from app.src.persistence.database import Database
from app.src.persistence.tables import (
    AccountRecord,
    CallbackDeliveryRecord,
    MaterialRecord,
    TaskEventRecord,
    TaskMaterialRecord,
    TaskRecord,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _queue_callback(
    session,
    task: TaskRecord,
    event_type: str,
    occurred_at: datetime,
    *,
    verification_expires_at: datetime | None = None,
) -> None:
    callback_url = task.payload.get("callback_url")
    if not callback_url:
        return
    event_id = uuid4().hex
    error = (
        {
            "code": task.error_code,
            "message": task.error_message,
            "details": task.error_details or {},
        }
        if task.error_code or task.error_message
        else None
    )
    session.add(
        CallbackDeliveryRecord(
            id=event_id,
            task_id=task.id,
            callback_url=str(callback_url),
            event_type=event_type,
            payload={
                "event_id": event_id,
                "event": event_type,
                "task_id": task.id,
                "operation": task.operation,
                "account": task.account,
                "status": task.status,
                "stage": task.stage,
                "result": task.result,
                "error": error,
                "verification_expires_at": iso_utc(verification_expires_at),
                "occurred_at": iso_utc(occurred_at),
            },
            next_attempt_at=occurred_at,
        )
    )


class Repository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def upsert_account(self, account: str, cookie_path: str | None, status: str) -> AccountRecord:
        async with self.database.session_factory() as session:
            record = await session.get(AccountRecord, account)
            now = utc_now()
            if record is None:
                record = AccountRecord(
                    account=account,
                    cookie_path=cookie_path,
                    status=status,
                    last_checked_at=now,
                )
                session.add(record)
            else:
                if cookie_path is not None:
                    record.cookie_path = cookie_path
                record.status = status
                record.last_checked_at = now
                record.updated_at = now
            await session.commit()
            await session.refresh(record)
            return record

    async def get_account(self, account: str) -> AccountRecord | None:
        async with self.database.session_factory() as session:
            return await session.get(AccountRecord, account)

    async def get_material(self, material_id: str) -> MaterialRecord | None:
        async with self.database.session_factory() as session:
            return await session.get(MaterialRecord, material_id)

    async def get_material_for_account(self, material_id: str, account: str) -> MaterialRecord | None:
        async with self.database.session_factory() as session:
            result = await session.execute(
                select(MaterialRecord).where(
                    MaterialRecord.id == material_id,
                    MaterialRecord.account == account,
                )
            )
            return result.scalar_one_or_none()

    async def get_material_by_hash(self, account: str, sha256: str) -> MaterialRecord | None:
        async with self.database.session_factory() as session:
            result = await session.execute(
                select(MaterialRecord).where(
                    MaterialRecord.account == account,
                    MaterialRecord.sha256 == sha256,
                )
            )
            return result.scalar_one_or_none()

    async def add_material(self, record: MaterialRecord) -> MaterialRecord:
        async with self.database.session_factory() as session:
            session.add(record)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await self.get_material_by_hash(record.account, record.sha256)
                if existing is None:
                    raise
                return existing
            await session.refresh(record)
            return record

    async def delete_material_record(self, material_id: str, account: str) -> bool:
        async with self.database.session_factory() as session:
            result = await session.execute(
                delete(MaterialRecord).where(
                    MaterialRecord.id == material_id,
                    MaterialRecord.account == account,
                )
            )
            await session.commit()
            return bool(result.rowcount)

    async def material_has_active_task(self, material_id: str) -> bool:
        statuses = [status.value for status in ACTIVE_TASK_STATUSES]
        async with self.database.session_factory() as session:
            statement = select(
                exists().where(
                    TaskMaterialRecord.material_id == material_id,
                    TaskMaterialRecord.task_id == TaskRecord.id,
                    TaskRecord.status.in_(statuses),
                )
            )
            return bool((await session.execute(statement)).scalar())

    async def create_task(
        self,
        *,
        task_id: str,
        account: str,
        operation: str,
        payload: dict,
        material_ids: Iterable[str] = (),
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> TaskRecord:
        async with self.database.session_factory() as session:
            task = TaskRecord(
                id=task_id,
                account=account,
                operation=operation,
                status=TaskStatus.QUEUED.value,
                stage=TaskStage.QUEUED.value,
                payload=payload,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            session.add(task)
            try:
                # No ORM relationship is needed here, so make the FK ordering explicit.
                await session.flush()
                session.add(
                    TaskEventRecord(
                        id=uuid4().hex,
                        task_id=task_id,
                        stage=TaskStage.QUEUED.value,
                        message="任务已进入队列",
                    )
                )
                for material_id in dict.fromkeys(material_ids):
                    session.add(TaskMaterialRecord(task_id=task_id, material_id=material_id))
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise
            await session.refresh(task)
            return task

    async def find_idempotent_task(self, account: str, operation: str, key: str) -> TaskRecord | None:
        async with self.database.session_factory() as session:
            result = await session.execute(
                select(TaskRecord).where(
                    TaskRecord.account == account,
                    TaskRecord.operation == operation,
                    TaskRecord.idempotency_key == key,
                )
            )
            return result.scalar_one_or_none()

    async def get_task(self, task_id: str, account: str | None = None) -> TaskRecord | None:
        async with self.database.session_factory() as session:
            statement = select(TaskRecord).where(TaskRecord.id == task_id)
            if account is not None:
                statement = statement.where(TaskRecord.account == account)
            result = await session.execute(statement)
            return result.scalar_one_or_none()

    async def list_queued_tasks(
        self,
        limit: int = 100,
        operations: Sequence[str] | None = None,
    ) -> list[TaskRecord]:
        async with self.database.session_factory() as session:
            statement = select(TaskRecord).where(TaskRecord.status == TaskStatus.QUEUED.value)
            if operations:
                statement = statement.where(TaskRecord.operation.in_(operations))
            result = await session.execute(
                statement.order_by(TaskRecord.created_at.asc()).limit(limit)
            )
            return list(result.scalars())

    async def claim_task(self, task_id: str) -> bool:
        now = utc_now()
        async with self.database.session_factory() as session:
            result = await session.execute(
                update(TaskRecord)
                .where(
                    TaskRecord.id == task_id,
                    TaskRecord.status == TaskStatus.QUEUED.value,
                    TaskRecord.cancel_requested.is_(False),
                )
                .values(
                    status=TaskStatus.RUNNING.value,
                    stage=TaskStage.VALIDATING_ACCOUNT.value,
                    started_at=now,
                    updated_at=now,
                )
            )
            await session.commit()
            return bool(result.rowcount)

    async def add_task_event(self, task_id: str, stage: str, message: str) -> None:
        async with self.database.session_factory() as session:
            session.add(
                TaskEventRecord(
                    id=uuid4().hex,
                    task_id=task_id,
                    stage=stage,
                    message=message,
                )
            )
            await session.commit()

    async def update_task_progress(
        self,
        task_id: str,
        stage: str,
        message: str,
        *,
        verification_expires_at: datetime | None = None,
    ) -> None:
        status = (
            TaskStatus.WAITING_VERIFICATION.value
            if stage == TaskStage.WAITING_VERIFICATION.value
            else TaskStatus.RUNNING.value
        )
        now = utc_now()
        async with self.database.session_factory() as session:
            task = await session.get(TaskRecord, task_id)
            if task is None:
                return
            task.stage = stage
            task.status = status
            task.updated_at = now
            if stage == TaskStage.PUBLISHING.value:
                task.may_have_published = True
            session.add(
                TaskEventRecord(
                    id=uuid4().hex,
                    task_id=task_id,
                    stage=stage,
                    message=message,
                )
            )
            if stage == TaskStage.WAITING_VERIFICATION.value:
                _queue_callback(
                    session,
                    task,
                    TaskStatus.WAITING_VERIFICATION.value,
                    now,
                    verification_expires_at=verification_expires_at,
                )
            await session.commit()

    async def finish_task(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        result: dict | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        error_details: dict | None = None,
    ) -> None:
        now = utc_now()
        stage = TaskStage.COMPLETED.value if status == TaskStatus.SUCCEEDED else status.value
        async with self.database.session_factory() as session:
            task = await session.get(TaskRecord, task_id)
            if task is None:
                return
            task.status = status.value
            task.stage = stage
            task.result = result
            task.error_code = error_code
            task.error_message = error_message
            task.error_details = error_details
            task.finished_at = now
            task.updated_at = now
            session.add(
                TaskEventRecord(
                    id=uuid4().hex,
                    task_id=task_id,
                    stage=stage,
                    message=error_message or ("任务执行成功" if status == TaskStatus.SUCCEEDED else f"任务已{status.value}"),
                )
            )
            _queue_callback(session, task, status.value, now)
            await session.commit()

    async def request_task_cancel(self, task_id: str, account: str) -> TaskRecord | None:
        async with self.database.session_factory() as session:
            task = (
                await session.execute(
                    select(TaskRecord).where(TaskRecord.id == task_id, TaskRecord.account == account)
                )
            ).scalar_one_or_none()
            if task is None:
                return None
            task.cancel_requested = True
            task.updated_at = utc_now()
            if task.status == TaskStatus.QUEUED.value:
                task.status = TaskStatus.CANCELLED.value
                task.stage = TaskStatus.CANCELLED.value
                task.finished_at = utc_now()
            session.add(
                TaskEventRecord(
                    id=uuid4().hex,
                    task_id=task.id,
                    stage=task.stage,
                    message=(
                        "任务已取消"
                        if task.status == TaskStatus.CANCELLED.value
                        else "已请求取消任务"
                    ),
                )
            )
            if task.status == TaskStatus.CANCELLED.value:
                _queue_callback(
                    session,
                    task,
                    TaskStatus.CANCELLED.value,
                    task.finished_at or utc_now(),
                )
            await session.commit()
            await session.refresh(task)
            return task

    async def list_task_events(self, task_id: str, limit: int = 50) -> list[TaskEventRecord]:
        async with self.database.session_factory() as session:
            result = await session.execute(
                select(TaskEventRecord)
                .where(TaskEventRecord.task_id == task_id)
                .order_by(TaskEventRecord.created_at.desc())
                .limit(limit)
            )
            return list(reversed(list(result.scalars())))

    async def mark_inflight_interrupted(self) -> list[TaskRecord]:
        now = utc_now()
        async with self.database.session_factory() as session:
            tasks = list(
                (
                    await session.execute(
                        select(TaskRecord).where(
                            TaskRecord.status.in_(
                                [TaskStatus.RUNNING.value, TaskStatus.WAITING_VERIFICATION.value]
                            )
                        )
                    )
                ).scalars()
            )
            for task in tasks:
                task.status = TaskStatus.INTERRUPTED.value
                task.stage = TaskStatus.INTERRUPTED.value
                task.error_code = "SERVICE_RESTARTED"
                task.error_message = "服务重启导致任务中断，发布结果未知"
                task.finished_at = now
                task.updated_at = now
                session.add(
                    TaskEventRecord(
                        id=uuid4().hex,
                        task_id=task.id,
                        stage=task.stage,
                        message=task.error_message,
                    )
                )
                _queue_callback(session, task, TaskStatus.INTERRUPTED.value, now)
            await session.commit()
            return tasks

    async def reset_inflight_callbacks(self) -> None:
        async with self.database.session_factory() as session:
            await session.execute(
                update(CallbackDeliveryRecord)
                .where(CallbackDeliveryRecord.status == "delivering")
                .values(status="pending", next_attempt_at=utc_now(), updated_at=utc_now())
            )
            await session.commit()

    async def list_due_callbacks(self, limit: int = 100) -> list[CallbackDeliveryRecord]:
        earlier = aliased(CallbackDeliveryRecord)
        async with self.database.session_factory() as session:
            result = await session.execute(
                select(CallbackDeliveryRecord)
                .where(
                    CallbackDeliveryRecord.status == "pending",
                    CallbackDeliveryRecord.next_attempt_at <= utc_now(),
                    ~exists(
                        select(earlier.id).where(
                            earlier.task_id == CallbackDeliveryRecord.task_id,
                            earlier.created_at < CallbackDeliveryRecord.created_at,
                            earlier.status.in_(["pending", "delivering"]),
                        )
                    ),
                )
                .order_by(CallbackDeliveryRecord.next_attempt_at.asc())
                .limit(limit)
            )
            return list(result.scalars())

    async def claim_callback(self, event_id: str) -> CallbackDeliveryRecord | None:
        now = utc_now()
        async with self.database.session_factory() as session:
            result = await session.execute(
                update(CallbackDeliveryRecord)
                .where(
                    CallbackDeliveryRecord.id == event_id,
                    CallbackDeliveryRecord.status == "pending",
                    CallbackDeliveryRecord.next_attempt_at <= now,
                )
                .values(
                    status="delivering",
                    attempts=CallbackDeliveryRecord.attempts + 1,
                    updated_at=now,
                )
            )
            if not result.rowcount:
                await session.rollback()
                return None
            await session.commit()
            return await session.get(CallbackDeliveryRecord, event_id)

    async def finish_callback_attempt(
        self,
        event_id: str,
        *,
        delivered: bool,
        last_error: str | None = None,
        next_attempt_at: datetime | None = None,
        exhausted: bool = False,
    ) -> None:
        now = utc_now()
        async with self.database.session_factory() as session:
            delivery = await session.get(CallbackDeliveryRecord, event_id)
            if delivery is None:
                return
            delivery.status = "delivered" if delivered else ("dead" if exhausted else "pending")
            delivery.last_error = last_error
            delivery.delivered_at = now if delivered else None
            delivery.next_attempt_at = next_attempt_at or now
            delivery.updated_at = now
            await session.commit()

    async def list_task_callbacks(self, task_id: str) -> list[CallbackDeliveryRecord]:
        async with self.database.session_factory() as session:
            result = await session.execute(
                select(CallbackDeliveryRecord)
                .where(CallbackDeliveryRecord.task_id == task_id)
                .order_by(CallbackDeliveryRecord.created_at.asc())
            )
            return list(result.scalars())

    async def cleanup_terminal_tasks(self, retention_days: int) -> int:
        cutoff = utc_now() - timedelta(days=retention_days)
        terminal = [
            TaskStatus.SUCCEEDED.value,
            TaskStatus.FAILED.value,
            TaskStatus.CANCELLED.value,
            TaskStatus.INTERRUPTED.value,
        ]
        async with self.database.session_factory() as session:
            result = await session.execute(
                delete(TaskRecord).where(
                    TaskRecord.status.in_(terminal),
                    TaskRecord.finished_at.is_not(None),
                    TaskRecord.finished_at < cutoff,
                )
            )
            await session.commit()
            return int(result.rowcount or 0)
