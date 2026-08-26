"""Model-invocation ledger (FR-053).

One row per model call, not per answer: a single grounded answer invokes a model
up to four times — understanding, query embedding, reranking, generation — and
recording them separately is the difference between knowing *answers are slow*
and knowing *reranking is slow*.

Written asynchronously for the same reason the audit writer is: four synchronous
inserts on the hot path would add latency for data nobody reads in real time.
The difference from audit is what happens on failure — a dropped usage row is
acceptable, a dropped audit row is not.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from types import TracebackType
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

_log = logging.getLogger(__name__)

Operation = Literal["understand", "embed_query", "embed_document", "rerank", "chat"]

_INSERT = text("""
    INSERT INTO model_invocations
        (operation, provider, model, message_id, ingestion_run_id, user_id,
         department, input_tokens, output_tokens, latency_ms, cached, outcome)
    VALUES
        (:operation, :provider, :model, CAST(:message_id AS uuid),
         CAST(:ingestion_run_id AS uuid), CAST(:user_id AS uuid), :department,
         :input_tokens, :output_tokens, :latency_ms, :cached, :outcome)
""")


@dataclass(slots=True)
class Invocation:
    operation: Operation
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int | None = None
    #: A cache hit is a real operation with real latency and zero tokens.
    #: Without this flag, consumption looks like it is falling when demand is flat.
    cached: bool = False
    outcome: str = "success"
    user_id: str | None = None
    department: str | None = None
    message_id: str | None = None
    ingestion_run_id: str | None = None


class UsageLedger:
    def __init__(self, engine: AsyncEngine, *, queue_size: int = 4000) -> None:
        self._engine = engine
        self._queue: asyncio.Queue[Invocation] = asyncio.Queue(maxsize=queue_size)
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
        while not self._queue.empty():
            await self._write(self._queue.get_nowait())

    def record(self, invocation: Invocation) -> None:
        try:
            self._queue.put_nowait(invocation)
        except asyncio.QueueFull:
            # Telemetry must never block an answer. The drop is logged so the
            # gap in the ledger is visible rather than silent.
            _log.warning("usage queue full; dropped %s", invocation.operation)

    def measure(self, operation: Operation, provider: str, model: str, **fields: object) -> _Timer:
        """Time a call and record it, whatever the outcome.

        ``with ledger.measure(...) as m: ... m.tokens(in_, out)`` — an exception
        still produces a row, marked ``error``. Usage that only records successes
        makes an outage look like a quiet day.
        """
        return _Timer(self, operation, provider, model, fields)

    async def _drain(self) -> None:
        while True:
            await self._write(await self._queue.get())

    async def _write(self, inv: Invocation) -> None:
        try:
            async with self._engine.begin() as conn:
                await conn.execute(
                    _INSERT,
                    {
                        "operation": inv.operation,
                        "provider": inv.provider,
                        "model": inv.model,
                        "message_id": inv.message_id,
                        "ingestion_run_id": inv.ingestion_run_id,
                        "user_id": inv.user_id,
                        "department": inv.department,
                        "input_tokens": inv.input_tokens,
                        "output_tokens": inv.output_tokens,
                        "latency_ms": inv.latency_ms,
                        "cached": inv.cached,
                        "outcome": inv.outcome,
                    },
                )
        except Exception as exc:
            _log.error("usage write failed for %s: %s", inv.operation, exc)


class _Timer:
    def __init__(
        self,
        ledger: UsageLedger,
        operation: Operation,
        provider: str,
        model: str,
        fields: dict[str, object],
    ) -> None:
        self._ledger = ledger
        self._inv = Invocation(operation=operation, provider=provider, model=model)
        for key, value in fields.items():
            setattr(self._inv, key, value)
        self._started = 0.0

    def tokens(self, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self._inv.input_tokens = input_tokens
        self._inv.output_tokens = output_tokens

    def cached(self, *, hit: bool = True) -> None:
        self._inv.cached = hit

    def __enter__(self) -> _Timer:
        self._started = time.perf_counter()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        self._inv.latency_ms = int((time.perf_counter() - self._started) * 1000)
        if exc_type is not None:
            self._inv.outcome = "timeout" if exc_type is TimeoutError else "error"
        self._ledger.record(self._inv)
        return False
