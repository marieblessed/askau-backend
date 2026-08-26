"""Append-only audit writer.

Two rules, both load-bearing:

* **Never blocks a request.** Writes are queued and flushed off the request
  path. An audit backlog degrades observability; a synchronous audit write on
  the hot path degrades the product.
* **Never records content.** FR-052 says sensitive content should not be
  unnecessarily replicated into logs. The stricter rule applied here is that an
  audit row records *that* a query happened and *which documents* were
  retrieved, never the question or the answer — an investigator can reconstruct
  what was accessed without reading what was asked.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from askau.audit.events import CATEGORY_OF, EventType
from askau.core.correlation import get_correlation_id
from askau.core.redaction import scrub
from askau.domain.enums import AuditOutcome

_log = logging.getLogger(__name__)

_INSERT = text("""
    INSERT INTO audit_events
        (correlation_id, event_type, event_category, outcome, actor_user_id,
         actor_email, resource_type, resource_id, ip_hash, user_agent, detail)
    VALUES
        (CAST(:correlation_id AS uuid), :event_type, :event_category,
         CAST(:outcome AS audit_outcome), CAST(:actor_user_id AS uuid),
         :actor_email, :resource_type, :resource_id, :ip_hash, :user_agent,
         CAST(:detail AS jsonb))
""")


@dataclass(slots=True)
class AuditEvent:
    event_type: EventType
    outcome: AuditOutcome = AuditOutcome.SUCCESS
    actor_user_id: str | None = None
    actor_email: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    ip_hash: bytes | None = None
    user_agent: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    correlation_id: str = ""


class AuditWriter:
    def __init__(self, engine: AsyncEngine, *, queue_size: int = 2000) -> None:
        self._engine = engine
        self._queue: asyncio.Queue[AuditEvent] = asyncio.Queue(maxsize=queue_size)
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._drain())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._flush_remaining()

    def record(self, event: AuditEvent) -> None:
        """Enqueue. Synchronous and non-failing by design."""
        event.correlation_id = event.correlation_id or get_correlation_id()
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            # Dropping is the lesser evil: blocking here would stall a user's
            # answer to record that the answer happened. The drop itself is
            # logged, so the gap is visible.
            _log.error("audit queue full; dropped %s", event.event_type)

    async def _drain(self) -> None:
        while True:
            event = await self._queue.get()
            await self._write(event)

    async def _flush_remaining(self) -> None:
        while not self._queue.empty():
            await self._write(self._queue.get_nowait())

    async def _write(self, event: AuditEvent) -> None:
        import json

        try:
            async with self._engine.begin() as conn:
                await conn.execute(
                    _INSERT,
                    {
                        "correlation_id": event.correlation_id,
                        "event_type": event.event_type.value,
                        "event_category": CATEGORY_OF[event.event_type].value,
                        "outcome": event.outcome.value,
                        "actor_user_id": event.actor_user_id,
                        "actor_email": event.actor_email,
                        "resource_type": event.resource_type,
                        "resource_id": event.resource_id,
                        "ip_hash": event.ip_hash,
                        "user_agent": event.user_agent,
                        "detail": json.dumps(scrub(event.detail)),
                    },
                )
        except Exception as exc:
            _log.error("audit write failed for %s: %s", event.event_type, exc)
