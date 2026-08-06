from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable
from uuid import uuid4

from sqlalchemy import delete, exists, select, update
from sqlalchemy.exc import IntegrityError

from app.src.domain.states import ACTIVE_TASK_STATUSES, TaskStage, TaskStatus
from app.src.persistence.database import Database
from app.src.persistence.tables import (
    AccountRecord,
    MaterialRecord,
    TaskEventRecord,
    TaskMaterialRecord,
    TaskRecord,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


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

    async def list_queued_tasks(self, limit: int = 100) -> list[TaskRecord]:
        async with self.database.session_factory() as session:
            result = await session.execute(
                select(TaskRecord)
                .where(TaskRecord.status == TaskStatus.QUEUED.value)
                .order_by(TaskRecord.created_at.asc())
                .limit(limit)
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

    async def update_task_progress(self, task_id: str, stage: str, message: str) -> None:
        status = (
            TaskStatus.WAITING_VERIFICATION.value
            if stage == TaskStage.WAITING_VERIFICATION.value
            else TaskStatus.RUNNING.value
        )
        now = utc_now()
        values = {
            "stage": stage,
            "status": status,
            "updated_at": now,
        }
        if stage == TaskStage.PUBLISHING.value:
            values["may_have_published"] = True
        async with self.database.session_factory() as session:
            await session.execute(
                update(TaskRecord)
                .where(TaskRecord.id == task_id)
                .values(**values)
            )
            session.add(
                TaskEventRecord(
                    id=uuid4().hex,
                    task_id=task_id,
                    stage=stage,
                    message=message,
                )
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
            await session.execute(
                update(TaskRecord)
                .where(TaskRecord.id == task_id)
                .values(
                    status=status.value,
                    stage=stage,
                    result=result,
                    error_code=error_code,
                    error_message=error_message,
                    error_details=error_details,
                    finished_at=now,
                    updated_at=now,
                )
            )
            session.add(
                TaskEventRecord(
                    id=uuid4().hex,
                    task_id=task_id,
                    stage=stage,
                    message=error_message or ("任务执行成功" if status == TaskStatus.SUCCEEDED else f"任务已{status.value}"),
                )
            )
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

    async def mark_inflight_interrupted(self) -> int:
        now = utc_now()
        async with self.database.session_factory() as session:
            result = await session.execute(
                update(TaskRecord)
                .where(
                    TaskRecord.status.in_(
                        [TaskStatus.RUNNING.value, TaskStatus.WAITING_VERIFICATION.value]
                    )
                )
                .values(
                    status=TaskStatus.INTERRUPTED.value,
                    stage=TaskStatus.INTERRUPTED.value,
                    error_code="SERVICE_RESTARTED",
                    error_message="服务重启导致任务中断，发布结果未知",
                    finished_at=now,
                    updated_at=now,
                )
            )
            await session.commit()
            return int(result.rowcount or 0)

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
