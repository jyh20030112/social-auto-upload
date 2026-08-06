from __future__ import annotations

import asyncio
import unittest

from app.src.services.verification import VerificationHub


class VerificationHubTest(unittest.IsolatedAsyncioTestCase):
    async def test_only_one_code_is_accepted_for_an_active_waiter(self) -> None:
        hub = VerificationHub()
        waiter = asyncio.create_task(hub.wait("task", timeout_seconds=1))
        await asyncio.sleep(0)

        self.assertTrue(await hub.submit("task", "123456"))
        self.assertFalse(await hub.submit("task", "654321"))
        self.assertEqual(await waiter, "123456")

    async def test_code_can_arrive_just_before_waiter(self) -> None:
        hub = VerificationHub()
        self.assertTrue(await hub.submit("task", "123456"))
        self.assertFalse(await hub.submit("task", "654321"))
        self.assertEqual(await hub.wait("task", timeout_seconds=1), "123456")


if __name__ == "__main__":
    unittest.main()
