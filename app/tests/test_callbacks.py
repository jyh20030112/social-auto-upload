from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from app.src.config import Settings
from app.src.domain.states import TaskStage, TaskStatus
from app.src.persistence.database import Database
from app.src.persistence.repositories import Repository
from app.src.services.accounts import AccountService
from app.src.services.callback_worker import CallbackWorker
from app.src.services.material_worker import MaterialWorker
from app.src.services.materials import MaterialService
from app.src.services.tasks import TaskService
from app.src.services.verification import VerificationHub


class CallbackWorkerTest(unittest.IsolatedAsyncioTestCase):
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

    async def asyncTearDown(self) -> None:
        await self.database.close()
        self.temporary.cleanup()

    async def test_waiting_and_final_events_are_persisted_and_delivered(self) -> None:
        task = await self.repository.create_task(
            task_id="a" * 32,
            account="alice",
            operation="publish_video",
            payload={"callback_url": "https://callback.example.com/result"},
        )
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        await self.repository.update_task_progress(
            task.id,
            TaskStage.WAITING_VERIFICATION.value,
            "等待短信验证码",
            verification_expires_at=expires_at,
        )
        await self.repository.finish_task(
            task.id,
            TaskStatus.SUCCEEDED,
            result={"published": True},
        )

        callbacks = await self.repository.list_task_callbacks(task.id)
        self.assertEqual([item.event_type for item in callbacks], ["waiting_verification", "succeeded"])
        self.assertEqual(callbacks[0].payload["verification_expires_at"], expires_at.isoformat().replace("+00:00", "Z"))
        self.assertEqual(callbacks[1].payload["result"], {"published": True})
        due = await self.repository.list_due_callbacks()
        self.assertEqual([item.id for item in due], [callbacks[0].id])

        received: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            received.append(request)
            return httpx.Response(204)

        worker = CallbackWorker(self.settings, self.repository)
        worker._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            claimed = await self.repository.claim_callback(callbacks[0].id)
            self.assertIsNotNone(claimed)
            await worker._deliver(claimed)
        finally:
            await worker._client.aclose()

        delivered = (await self.repository.list_task_callbacks(task.id))[0]
        self.assertEqual(delivered.status, "delivered")
        self.assertEqual(delivered.attempts, 1)
        self.assertEqual(received[0].headers["X-Callback-Event-ID"], callbacks[0].id)
        self.assertEqual(received[0].headers["X-Task-ID"], task.id)

    async def test_synchronous_wait_returns_business_result_or_verification_state(self) -> None:
        service = TaskService(
            self.settings.with_overrides(worker_enabled=True),
            self.repository,
            AccountService(self.settings, self.repository),
            VerificationHub(),
        )
        succeeded_task = await self.repository.create_task(
            task_id="b" * 32,
            account="alice",
            operation="login",
            payload={},
        )

        async def finish() -> None:
            await asyncio.sleep(0.05)
            await self.repository.finish_task(
                succeeded_task.id,
                TaskStatus.SUCCEEDED,
                result={"valid": True},
            )

        finisher = asyncio.create_task(finish())
        data, status_code = await service.wait_for_result(succeeded_task)
        await finisher
        self.assertEqual(status_code, 200)
        self.assertEqual(data, {"valid": True})

        waiting_task = await self.repository.create_task(
            task_id="c" * 32,
            account="alice",
            operation="publish_note",
            payload={},
        )
        await self.repository.update_task_progress(
            waiting_task.id,
            TaskStage.WAITING_VERIFICATION.value,
            "等待短信验证码",
        )
        current = await self.repository.get_task(waiting_task.id)
        data, status_code = await service.wait_for_result(current)
        self.assertEqual(status_code, 202)
        self.assertEqual(data["task_id"], waiting_task.id)
        self.assertEqual(data["status"], "waiting_verification")
        self.assertIsNotNone(data["verification_expires_at"])

    async def test_material_worker_processes_staged_files_and_queues_callback(self) -> None:
        task_id = "d" * 32
        staging_dir = self.settings.task_staging_dir / task_id
        staging_dir.mkdir()
        staged_path = staging_dir / "00-video.mp4"
        staged_path.write_bytes(b"video-content")
        task = await self.repository.create_task(
            task_id=task_id,
            account="alice",
            operation="upload_materials",
            payload={
                "callback_url": "https://callback.example.com/materials",
                "staging_dir": str(staging_dir),
                "items": [
                    {
                        "filename": "video.mp4",
                        "content_type": "video/mp4",
                        "staged_path": str(staged_path),
                    }
                ],
            },
        )
        self.assertTrue(await self.repository.claim_task(task.id))
        worker = MaterialWorker(
            self.settings,
            self.repository,
            MaterialService(self.settings, self.repository),
        )
        await worker._run_claimed(task.id)

        finished = await self.repository.get_task(task.id)
        self.assertEqual(finished.status, "succeeded")
        self.assertEqual(finished.result["succeeded_count"], 1)
        self.assertFalse(staging_dir.exists())
        callbacks = await self.repository.list_task_callbacks(task.id)
        self.assertEqual([item.event_type for item in callbacks], ["succeeded"])


if __name__ == "__main__":
    unittest.main()
