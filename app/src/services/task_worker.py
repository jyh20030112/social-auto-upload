from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.src.config import Settings
from app.src.domain.states import TaskOperation, TaskStage, TaskStatus
from app.src.persistence.repositories import Repository
from app.src.persistence.tables import TaskRecord
from app.src.services.accounts import AccountService
from app.src.services.publisher import PublisherService
from app.src.services.verification import VerificationHub


class TaskWorker:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        accounts: AccountService,
        publisher: PublisherService,
        verification: VerificationHub,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.accounts = accounts
        self.publisher = publisher
        self.verification = verification
        self._dispatcher: asyncio.Task | None = None
        self._cleaner: asyncio.Task | None = None
        self._running: dict[str, asyncio.Task] = {}
        self._active_accounts: set[str] = set()
        self._stopping = False

    @property
    def healthy(self) -> bool:
        return not self._stopping and self._dispatcher is not None and not self._dispatcher.done()

    async def start(self) -> None:
        self._stopping = False
        await self.repository.mark_inflight_interrupted()
        self._dispatcher = asyncio.create_task(self._dispatch_loop(), name="douyin-task-dispatcher")
        self._cleaner = asyncio.create_task(self._cleanup_loop(), name="douyin-task-cleaner")

    async def stop(self) -> None:
        self._stopping = True
        for background in (self._dispatcher, self._cleaner):
            if background is not None:
                background.cancel()
        await asyncio.gather(
            *[item for item in (self._dispatcher, self._cleaner) if item is not None],
            return_exceptions=True,
        )

        if self._running:
            done, pending = await asyncio.wait(
                list(self._running.values()),
                timeout=self.settings.shutdown_grace_seconds,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    def cancel_running(self, task_id: str) -> None:
        running = self._running.get(task_id)
        if running is not None:
            running.cancel()

    async def _dispatch_loop(self) -> None:
        try:
            while not self._stopping:
                self._remove_finished()
                capacity = self.settings.max_browser_tasks - len(self._running)
                if capacity > 0:
                    queued = await self.repository.list_queued_tasks()
                    for record in queued:
                        if capacity <= 0:
                            break
                        if record.account in self._active_accounts:
                            continue
                        if await self.repository.claim_task(record.id):
                            self._active_accounts.add(record.account)
                            task = asyncio.create_task(
                                self._run_claimed(record.id),
                                name=f"douyin-task-{record.id}",
                            )
                            self._running[record.id] = task
                            capacity -= 1
                await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            return

    def _remove_finished(self) -> None:
        for task_id, running in list(self._running.items()):
            if not running.done():
                continue
            self._running.pop(task_id, None)

    async def _run_claimed(self, task_id: str) -> None:
        record = await self.repository.get_task(task_id)
        if record is None:
            return
        try:
            timeout = self._timeout_for(record.operation)
            async with asyncio.timeout(timeout):
                result = await self._execute(record)
            await self.repository.finish_task(task_id, TaskStatus.SUCCEEDED, result=result)
        except asyncio.CancelledError:
            latest = await self.repository.get_task(task_id)
            status = (
                TaskStatus.INTERRUPTED
                if latest is not None and latest.may_have_published
                else TaskStatus.CANCELLED
            )
            await self.repository.finish_task(
                task_id,
                status,
                error_code="TASK_CANCELLED",
                error_message=(
                    "任务取消时可能已经提交发布，结果未知"
                    if status == TaskStatus.INTERRUPTED
                    else "任务已取消"
                ),
            )
        except TimeoutError:
            latest = await self.repository.get_task(task_id)
            status = (
                TaskStatus.INTERRUPTED
                if latest is not None and latest.may_have_published
                else TaskStatus.FAILED
            )
            await self.repository.finish_task(
                task_id,
                status,
                error_code="TASK_TIMEOUT",
                error_message=(
                    "任务超时，且可能已经提交发布，结果未知"
                    if status == TaskStatus.INTERRUPTED
                    else "任务执行超时"
                ),
            )
        except Exception as exc:
            latest = await self.repository.get_task(task_id)
            status = (
                TaskStatus.INTERRUPTED
                if latest is not None and latest.may_have_published
                else TaskStatus.FAILED
            )
            await self.repository.finish_task(
                task_id,
                status,
                error_code="TASK_EXECUTION_FAILED",
                error_message=str(exc) or exc.__class__.__name__,
                error_details={"exception_type": exc.__class__.__name__},
            )
        finally:
            await self.verification.cancel(task_id)
            self._active_accounts.discard(record.account)

    def _timeout_for(self, operation: str) -> int:
        if operation == TaskOperation.LOGIN.value:
            return self.settings.login_timeout_seconds
        if operation == TaskOperation.PUBLISH_VIDEO.value:
            return self.settings.video_timeout_seconds
        return self.settings.note_timeout_seconds

    async def _execute(self, record: TaskRecord) -> dict:
        async def progress(stage: str, message: str) -> None:
            await self.repository.update_task_progress(record.id, stage, message)

        async def verification_provider() -> str:
            return await self.verification.wait(
                record.id,
                self.settings.verification_timeout_seconds,
            )

        await progress(TaskStage.VALIDATING_ACCOUNT.value, "正在校验账号和任务参数")
        if record.operation == TaskOperation.LOGIN.value:
            await progress(TaskStage.LAUNCHING_BROWSER.value, "正在验证导入的 Cookie")
            result = await self.accounts.execute_login(
                record.account,
                record.payload["temporary_cookie_path"],
            )
            await progress(TaskStage.PERSISTING_COOKIE.value, "Cookie 已验证并长期保存")
            return result
        if record.operation == TaskOperation.PUBLISH_VIDEO.value:
            return await self.publisher.publish_video(
                record.account,
                record.payload,
                progress,
                verification_provider,
            )
        if record.operation == TaskOperation.PUBLISH_NOTE.value:
            return await self.publisher.publish_note(
                record.account,
                record.payload,
                progress,
                verification_provider,
            )
        raise RuntimeError(f"不支持的任务类型: {record.operation}")

    async def _cleanup_loop(self) -> None:
        try:
            while not self._stopping:
                await self.repository.cleanup_terminal_tasks(self.settings.terminal_retention_days)
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            return
