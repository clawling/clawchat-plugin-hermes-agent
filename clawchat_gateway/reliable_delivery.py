"""Connection-local dense delivery acknowledgements (Protocol v2 §11.7)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from clawchat_gateway.protocol import encode_frame

logger = logging.getLogger(__name__)

ACK_DEBOUNCE_SECONDS = 0.2
ACK_RESEND_SECONDS = 30.0
ACK_FLUSH_TIMEOUT_SECONDS = 1.0
RECEIPT_RETRY_SECONDS = 30.0
RECEIPT_RECOVERY_TIMEOUT_SECONDS = 10.0
INBOUND_RETRYABLE = object()


class DseqAckTracker:
    def __init__(self, ws: Any, epoch: str, is_current: Callable[[], bool]) -> None:
        self.epoch = epoch
        self.last_read_dseq = 0
        self.acked = 0
        self.disposed = False
        self._ws = ws
        self._is_current = is_current
        self._pending: set[int] = set()
        self._debounce: asyncio.Task[None] | None = None
        self._periodic: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._last_sent_at = asyncio.get_running_loop().time()

    def current(self) -> bool:
        return not self.disposed and self._is_current()

    def received(self) -> None:
        """Start the resend clock on the first received dseq, even a failed write."""
        if self.current() and self._periodic is None:
            self._last_sent_at = asyncio.get_running_loop().time()
            self._periodic = asyncio.create_task(self._resend())

    def record(self, dseq: int) -> None:
        if not self.current():
            return
        self.received()
        if dseq > self.acked:
            self._pending.add(dseq)
        before = self.acked
        while self.acked + 1 in self._pending:
            self.acked += 1
            self._pending.remove(self.acked)
        # Arrival also drives a flush if suspension delayed the periodic timer.
        due = asyncio.get_running_loop().time() - self._last_sent_at >= ACK_RESEND_SECONDS
        if self.acked and due:
            if self._debounce is not None:
                self._debounce.cancel()
            self._debounce = asyncio.create_task(self._delayed_flush(0))
        elif self.acked > before and (self._debounce is None or self._debounce.done()):
            # Keep the first deadline while progress continues; send the latest watermark.
            self._debounce = asyncio.create_task(self._delayed_flush(ACK_DEBOUNCE_SECONDS))

    async def _delayed_flush(self, delay: float) -> None:
        await asyncio.sleep(delay)
        if self.current():
            await self.flush_now()

    async def _resend(self) -> None:
        while self.current():
            await asyncio.sleep(ACK_RESEND_SECONDS)
            if self.current():
                await self.flush_now()

    async def flush_now(self) -> None:
        if not self.current() or not self.acked:
            return
        if self._debounce is not None and self._debounce is not asyncio.current_task():
            self._debounce.cancel()
            self._debounce = None
        async with self._send_lock:
            if not self.current() or not self.acked:
                return
            frame = {"version": "2", "event": "message.sync_ack", "trace_id": "sync-ack",
                     "payload": {"dseq": self.acked, "epoch": self.epoch}}
            try:
                await self._ws.send(encode_frame(frame))
            except Exception:  # Transport errors can contain credentials: omit their text.
                logger.warning("clawchat sync_ack send failed")
            else:
                if self.current():
                    self._last_sent_at = asyncio.get_running_loop().time()

    def dispose(self) -> None:
        self.disposed = True
        for task in (self._debounce, self._periodic):
            if task is not None and task is not asyncio.current_task():
                task.cancel()
        self._pending.clear()
