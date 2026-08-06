from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx

from app.src.config import Settings
from app.src.persistence.repositories import Repository, utc_now
from app.src.persistence.tables import CallbackDeliveryRecord


class CallbackWorker:
    MAX_ATTEMPTS = 6
    RETRY_DELAYS_SECONDS = (5, 30, 120, 600, 3600)

    def __init__(self, settings: Settings, repository: Repository) -> None:
        self.settings = settings
        self.repository = repository
        self._dispatcher: asyncio.Task | None = None
        self._running: dict[str, asyncio.Task] = {}
        self._stopping = False
        self._client: httpx.AsyncClient | None = None

    @property
    def healthy(self) -> bool:
        return not self._stopping and self._dispatcher is not None and not self._dispatcher.done()

    async def start(self) -> None:
        self._stopping = False
        await self.repository.reset_inflight_callbacks()
        self._client = httpx.AsyncClient(
            timeout=self.settings.callback_timeout_seconds,
            follow_redirects=False,
        )
        self._dispatcher = asyncio.create_task(self._dispatch_loop(), name="callback-dispatcher")

    async def stop(self) -> None:
        self._stopping = True
        if self._dispatcher is not None:
            self._dispatcher.cancel()
            await asyncio.gather(self._dispatcher, return_exceptions=True)
        for running in self._running.values():
            running.cancel()
        if self._running:
            await asyncio.gather(*self._running.values(), return_exceptions=True)
        if self._client is not None:
            await self._client.aclose()

    async def _dispatch_loop(self) -> None:
        try:
            while not self._stopping:
                self._remove_finished()
                capacity = self.settings.max_callback_tasks - len(self._running)
                if capacity > 0:
                    for candidate in await self.repository.list_due_callbacks(limit=capacity):
                        claimed = await self.repository.claim_callback(candidate.id)
                        if claimed is None:
                            continue
                        task = asyncio.create_task(
                            self._deliver(claimed),
                            name=f"callback-{claimed.id}",
                        )
                        self._running[claimed.id] = task
                await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            return

    def _remove_finished(self) -> None:
        for event_id, running in list(self._running.items()):
            if running.done():
                self._running.pop(event_id, None)

    async def _deliver(self, delivery: CallbackDeliveryRecord) -> None:
        if self._client is None:
            return
        error: str | None = None
        try:
            response = await self._client.post(
                delivery.callback_url,
                json=delivery.payload,
                headers={
                    "X-Callback-Event-ID": delivery.id,
                    "X-Task-ID": delivery.task_id,
                },
            )
            if 200 <= response.status_code < 300:
                await self.repository.finish_callback_attempt(delivery.id, delivered=True)
                return
            error = f"HTTP {response.status_code}"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = str(exc) or exc.__class__.__name__

        exhausted = delivery.attempts >= self.MAX_ATTEMPTS
        next_attempt_at = None
        if not exhausted:
            delay = self.RETRY_DELAYS_SECONDS[delivery.attempts - 1]
            next_attempt_at = utc_now() + timedelta(seconds=delay)
        await self.repository.finish_callback_attempt(
            delivery.id,
            delivered=False,
            last_error=(error or "callback delivery failed")[:1000],
            next_attempt_at=next_attempt_at,
            exhausted=exhausted,
        )
