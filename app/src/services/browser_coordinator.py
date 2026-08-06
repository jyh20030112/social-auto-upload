from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager


class BrowserCoordinator:
    """Coordinates the global browser limit and per-user platform account serialization."""

    def __init__(self, max_browser_tasks: int) -> None:
        self._semaphore = asyncio.Semaphore(max_browser_tasks)
        self._account_locks: defaultdict[tuple[str, str, str], asyncio.Lock] = defaultdict(
            asyncio.Lock
        )

    @asynccontextmanager
    async def slot(self, user_id: str, platform: str, account: str):
        key = (user_id, platform, account)
        account_lock = self._account_locks[key]
        async with account_lock:
            async with self._semaphore:
                yield
