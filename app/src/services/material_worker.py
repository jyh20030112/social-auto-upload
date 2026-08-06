from __future__ import annotations

import asyncio

from app.src.config import Settings
from app.src.domain.states import TaskOperation, TaskStage, TaskStatus
from app.src.persistence.repositories import Repository
from app.src.services.materials import MaterialService


class MaterialWorker:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        materials: MaterialService,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.materials = materials
        self._dispatcher: asyncio.Task | None = None
        self._running: dict[str, asyncio.Task] = {}
        self._stopping = False

    @property
    def healthy(self) -> bool:
        return not self._stopping and self._dispatcher is not None and not self._dispatcher.done()

    async def start(self) -> None:
        self._stopping = False
        self._dispatcher = asyncio.create_task(self._dispatch_loop(), name="material-dispatcher")

    async def stop(self) -> None:
        self._stopping = True
        if self._dispatcher is not None:
            self._dispatcher.cancel()
            await asyncio.gather(self._dispatcher, return_exceptions=True)
        if self._running:
            _done, pending = await asyncio.wait(
                self._running.values(),
                timeout=self.settings.shutdown_grace_seconds,
            )
            for running in pending:
                running.cancel()
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
                capacity = self.settings.max_material_tasks - len(self._running)
                if capacity > 0:
                    queued = await self.repository.list_queued_tasks(
                        limit=capacity,
                        operations=[TaskOperation.UPLOAD_MATERIALS.value],
                    )
                    for record in queued:
                        if await self.repository.claim_task(record.id):
                            running = asyncio.create_task(
                                self._run_claimed(record.id),
                                name=f"material-task-{record.id}",
                            )
                            self._running[record.id] = running
                await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            return

    def _remove_finished(self) -> None:
        for task_id, running in list(self._running.items()):
            if running.done():
                self._running.pop(task_id, None)

    async def _run_claimed(self, task_id: str) -> None:
        record = await self.repository.get_task(task_id)
        if record is None:
            return
        try:
            await self.repository.update_task_progress(
                task_id,
                TaskStage.PROCESSING_MATERIALS.value,
                "正在校验、去重并保存素材",
            )
            async with asyncio.timeout(self.settings.material_timeout_seconds):
                result = await self.materials.process_staged(record.user_id, record.payload)
            await self.repository.finish_task(task_id, TaskStatus.SUCCEEDED, result=result)
        except asyncio.CancelledError:
            self.materials.cleanup_staged(record.payload)
            await self.repository.finish_task(
                task_id,
                TaskStatus.CANCELLED,
                error_code="TASK_CANCELLED",
                error_message="素材任务已取消",
            )
        except TimeoutError:
            self.materials.cleanup_staged(record.payload)
            await self.repository.finish_task(
                task_id,
                TaskStatus.FAILED,
                error_code="TASK_TIMEOUT",
                error_message="素材处理超时",
            )
        except Exception as exc:
            self.materials.cleanup_staged(record.payload)
            await self.repository.finish_task(
                task_id,
                TaskStatus.FAILED,
                error_code="TASK_EXECUTION_FAILED",
                error_message=str(exc) or exc.__class__.__name__,
                error_details={"exception_type": exc.__class__.__name__},
            )
