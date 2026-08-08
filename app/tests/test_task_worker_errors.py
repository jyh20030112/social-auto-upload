from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.src.config import Settings
from app.src.domain.errors import ApiError
from app.src.persistence.database import Database
from app.src.persistence.repositories import Repository
from app.src.services.browser_coordinator import BrowserCoordinator
from app.src.services.task_worker import TaskWorker
from app.src.services.tasks import TaskService
from app.src.services.verification import VerificationHub


class _InvalidDouyinAccounts:
    async def execute_login(self, *_args, **_kwargs):
        raise ApiError(
            409,
            "DOUYIN_COOKIE_INVALID",
            "导入的抖音 cookie 已失效或缺少有效登录态",
            {
                "browser_diagnostic": {
                    "reason": "login_required",
                    "login_markers": ["phone_input"],
                }
            },
        )


class TaskWorkerErrorTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        data_dir = Path(self.temporary.name)
        self.settings = Settings(
            data_dir=data_dir,
            database_url=f"sqlite+aiosqlite:///{data_dir / 'test.db'}",
            worker_enabled=True,
        )
        self.settings.ensure_directories()
        self.database = Database(self.settings.database_url)
        await self.database.initialize()
        self.repository = Repository(self.database)

    async def asyncTearDown(self) -> None:
        await self.database.close()
        self.temporary.cleanup()

    async def test_login_api_error_keeps_diagnostic_and_task_id(self) -> None:
        task = await self.repository.create_task(
            task_id="e" * 32,
            user_id="user_a",
            platform="douyin",
            account="creator",
            operation="login",
            payload={"temporary_cookie_path": "/tmp/invalid-cookie.json"},
        )
        self.assertTrue(await self.repository.claim_task(task.id))
        accounts = _InvalidDouyinAccounts()
        verification = VerificationHub()
        worker = TaskWorker(
            self.settings,
            self.repository,
            accounts,
            object(),
            object(),
            object(),
            BrowserCoordinator(1),
            verification,
        )

        await worker._run_claimed(task.id)

        finished = await self.repository.get_task(task.id)
        self.assertEqual(finished.error_code, "DOUYIN_COOKIE_INVALID")
        self.assertEqual(
            finished.error_details["browser_diagnostic"]["reason"],
            "login_required",
        )

        service = TaskService(
            self.settings,
            self.repository,
            accounts,
            object(),
            verification,
        )
        with self.assertRaises(ApiError) as raised:
            await service.wait_for_result(finished)

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.code, "DOUYIN_COOKIE_INVALID")
        self.assertEqual(raised.exception.details["task_id"], task.id)
        self.assertEqual(
            raised.exception.details["browser_diagnostic"]["login_markers"],
            ["phone_input"],
        )
