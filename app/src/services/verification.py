from __future__ import annotations

import asyncio


class VerificationHub:
    def __init__(self) -> None:
        self._waiters: dict[str, asyncio.Future[str]] = {}
        self._pending_codes: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def wait(self, task_id: str, timeout_seconds: int) -> str:
        async with self._lock:
            pending = self._pending_codes.pop(task_id, None)
            if pending is not None:
                return pending
            loop = asyncio.get_running_loop()
            waiter: asyncio.Future[str] = loop.create_future()
            self._waiters[task_id] = waiter
        try:
            return await asyncio.wait_for(waiter, timeout=timeout_seconds)
        finally:
            async with self._lock:
                self._waiters.pop(task_id, None)
                self._pending_codes.pop(task_id, None)

    async def submit(self, task_id: str, code: str) -> bool:
        async with self._lock:
            if task_id in self._pending_codes:
                return False
            waiter = self._waiters.get(task_id)
            if waiter is not None:
                if waiter.done():
                    return False
                waiter.set_result(code)
                return True
            self._pending_codes[task_id] = code
            return True

    async def cancel(self, task_id: str) -> None:
        async with self._lock:
            waiter = self._waiters.pop(task_id, None)
            self._pending_codes.pop(task_id, None)
            if waiter is not None and not waiter.done():
                waiter.cancel()
