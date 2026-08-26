"""Conversation and message persistence.

Ownership is a predicate on every read, not a check the caller performs. FR-006
says conversation history is not visible to other users, and the reliable way to
hold that is for "someone else's conversation" and "no such conversation" to be
the same query result — so a missing `WHERE user_id` cannot leak, because there
is no code path that reads a conversation without one.

`messages` is range-partitioned on `created_at`, so its primary key is
`(id, created_at)`. Every foreign key into it carries both columns.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from askau.domain.answer import GroundedAnswer
from askau.domain.conversation import Citation
from askau.domain.enums import MessageRole


@dataclass(frozen=True, slots=True)
class StoredMessage:
    id: str
    created_at: datetime
    role: MessageRole
    seq: int
    content: str
    answer_state: str | None = None
    groundedness: float | None = None
    citations: tuple[Citation, ...] = ()
    correlation_id: str | None = None
    feedback: str | None = None


@dataclass(frozen=True, slots=True)
class StoredConversation:
    id: str
    title: str | None
    message_count: int
    last_message_at: datetime | None
    created_at: datetime


class ConversationRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    # ── conversations ───────────────────────────────────────────────────────

    async def create(self, user_id: str, title: str | None = None) -> str:
        async with self._engine.begin() as conn:
            return str(
                (
                    await conn.execute(
                        text("""
                        INSERT INTO conversations (user_id, title)
                        VALUES (CAST(:user_id AS uuid), :title)
                        RETURNING id
                        """),
                        {"user_id": user_id, "title": title},
                    )
                ).scalar_one()
            )

    async def list_for(self, user_id: str, limit: int = 50) -> list[StoredConversation]:
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        text("""
                    SELECT id::text, title, message_count, last_message_at, created_at
                    FROM conversations
                    WHERE user_id = CAST(:user_id AS uuid) AND NOT is_archived
                    ORDER BY coalesce(last_message_at, created_at) DESC
                    LIMIT :limit
                    """),
                        {"user_id": user_id, "limit": limit},
                    )
                )
                .mappings()
                .all()
            )
        return [StoredConversation(**dict(r)) for r in rows]

    async def owned_by(self, conversation_id: str, user_id: str) -> bool:
        """Ownership check for routes that mutate rather than read.

        Reads embed the predicate directly; this exists so a write can return
        404 before doing any work.
        """
        async with self._engine.connect() as conn:
            return (
                await conn.execute(
                    text("""
                    SELECT EXISTS (
                        SELECT 1 FROM conversations
                        WHERE id = CAST(:cid AS uuid) AND user_id = CAST(:uid AS uuid)
                    )
                    """),
                    {"cid": conversation_id, "uid": user_id},
                )
            ).scalar_one() is True

    async def delete(self, conversation_id: str, user_id: str) -> bool:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text("""
                DELETE FROM conversations
                WHERE id = CAST(:cid AS uuid) AND user_id = CAST(:uid AS uuid)
                """),
                {"cid": conversation_id, "uid": user_id},
            )
        return bool(result.rowcount)

    async def rename(
        self,
        conversation_id: str,
        user_id: str,
        title: str | None,
        is_archived: bool | None,
    ) -> bool:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text("""
                UPDATE conversations
                SET title = coalesce(:title, title),
                    is_archived = coalesce(:archived, is_archived)
                WHERE id = CAST(:cid AS uuid) AND user_id = CAST(:uid AS uuid)
                """),
                {"cid": conversation_id, "uid": user_id, "title": title, "archived": is_archived},
            )
        return bool(result.rowcount)

    async def withdraw_feedback(self, message_id: str, user_id: str) -> bool:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text("""
                DELETE FROM message_feedback
                WHERE message_id = CAST(:mid AS uuid) AND user_id = CAST(:uid AS uuid)
                """),
                {"mid": message_id, "uid": user_id},
            )
        return bool(result.rowcount)

    # ── messages ────────────────────────────────────────────────────────────

    async def history(
        self, conversation_id: str, user_id: str, limit: int = 100
    ) -> list[StoredMessage]:
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        text("""
                    SELECT m.id::text, m.created_at, m.role::text AS role, m.seq,
                           m.content, m.answer_state::text AS answer_state,
                           m.groundedness, m.correlation_id::text AS correlation_id,
                           f.rating::text AS feedback
                    FROM messages m
                    JOIN conversations c ON c.id = m.conversation_id
                    LEFT JOIN message_feedback f
                           ON f.message_id = m.id AND f.message_created_at = m.created_at
                    WHERE m.conversation_id = CAST(:cid AS uuid)
                      AND c.user_id = CAST(:uid AS uuid)
                    ORDER BY m.seq
                    LIMIT :limit
                    """),
                        {"cid": conversation_id, "uid": user_id, "limit": limit},
                    )
                )
                .mappings()
                .all()
            )

            messages = [
                StoredMessage(
                    id=r["id"],
                    created_at=r["created_at"],
                    role=MessageRole(r["role"]),
                    seq=r["seq"],
                    content=r["content"],
                    answer_state=r["answer_state"],
                    groundedness=float(r["groundedness"]) if r["groundedness"] else None,
                    correlation_id=r["correlation_id"],
                    feedback=r["feedback"],
                )
                for r in rows
            ]
            if not messages:
                return []

            cites = (
                (
                    await conn.execute(
                        text("""
                    SELECT c.message_id::text AS message_id, c.marker, c.chunk_id,
                           c.document_id::text AS document_id, c.rank, c.quote,
                           c.page_from, c.page_to, c.section_ref, c.verified,
                           d.title AS document_title, d.source_uri, d.version_label,
                           ks.name AS source_name
                    FROM citations c
                    JOIN documents d ON d.id = c.document_id
                    JOIN knowledge_sources ks ON ks.id = d.source_id
                    WHERE c.message_id = ANY(CAST(:ids AS uuid[]))
                    ORDER BY c.marker
                    """),
                        {"ids": [m.id for m in messages]},
                    )
                )
                .mappings()
                .all()
            )

        grouped: dict[str, list[Citation]] = {}
        for c in cites:
            grouped.setdefault(c["message_id"], []).append(
                Citation(
                    marker=c["marker"],
                    chunk_id=c["chunk_id"],
                    document_id=c["document_id"],
                    document_title=c["document_title"],
                    source_uri=c["source_uri"],
                    source_name=c["source_name"],
                    rank=c["rank"],
                    quote=c["quote"],
                    section_ref=c["section_ref"],
                    page_from=c["page_from"],
                    page_to=c["page_to"],
                    version_label=c["version_label"],
                    verified=c["verified"],
                )
            )
        # `replace`, not a dict rebuild: these are slots dataclasses and have
        # no __dict__ to splat.
        return [replace(m, citations=tuple(grouped.get(m.id, ()))) for m in messages]

    async def recent_turns(
        self, conversation_id: str, user_id: str, turns: int = 4
    ) -> list[tuple[str, str]]:
        """The last few exchanges, for follow-up resolution (FR-004).

        Bounded deliberately: unbounded history would grow the context window
        without bound and blur the current question with stale topics.
        """
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        text("""
                    SELECT m.role::text AS role, m.content
                    FROM messages m
                    JOIN conversations c ON c.id = m.conversation_id
                    WHERE m.conversation_id = CAST(:cid AS uuid)
                      AND c.user_id = CAST(:uid AS uuid)
                    ORDER BY m.seq DESC
                    LIMIT :limit
                    """),
                        {"cid": conversation_id, "uid": user_id, "limit": turns * 2},
                    )
                )
                .mappings()
                .all()
            )
        return [(r["role"], r["content"]) for r in reversed(rows)]

    async def append_turn(
        self,
        *,
        conversation_id: str,
        user_id: str,
        question: str,
        answer: GroundedAnswer,
        citations: tuple[Citation, ...],
        correlation_id: str,
        model_provider: str | None,
        model_name: str | None,
        ttft_ms: int | None,
        total_ms: int | None,
    ) -> str:
        """Persist the question and its answer as one atomic turn.

        Written in a single transaction: an answer stored without its question,
        or citations without their answer, would corrupt both the history and
        the quality metrics computed from it.
        """
        now = datetime.now(UTC)
        async with self._engine.begin() as conn:
            seq = (
                await conn.execute(
                    text("""
                    SELECT coalesce(max(seq), 0) FROM messages
                    WHERE conversation_id = CAST(:cid AS uuid)
                    """),
                    {"cid": conversation_id},
                )
            ).scalar_one()

            common: dict[str, Any] = {
                "cid": conversation_id,
                "uid": user_id,
                "corr": correlation_id,
                "now": now,
            }
            await conn.execute(
                text("""
                INSERT INTO messages
                    (conversation_id, user_id, role, seq, content, correlation_id, created_at)
                VALUES (CAST(:cid AS uuid), CAST(:uid AS uuid), 'user', :seq,
                        :content, CAST(:corr AS uuid), :now)
                """),
                {**common, "seq": seq + 1, "content": question},
            )
            message_id = str(
                (
                    await conn.execute(
                        text("""
                        INSERT INTO messages
                            (conversation_id, user_id, role, seq, content, answer_state,
                             model_provider, model_name, groundedness, retrieved_count,
                             ttft_ms, total_ms, correlation_id, created_at)
                        VALUES (CAST(:cid AS uuid), CAST(:uid AS uuid), 'assistant', :seq,
                                :content, CAST(:state AS answer_state), :provider, :model,
                                :groundedness, :retrieved, :ttft, :total,
                                CAST(:corr AS uuid), :now)
                        RETURNING id
                        """),
                        {
                            **common,
                            "seq": seq + 2,
                            "content": answer.content,
                            "state": answer.state.value,
                            "provider": model_provider,
                            "model": model_name,
                            "groundedness": answer.groundedness,
                            "retrieved": len(answer.retrieval) if answer.retrieval else 0,
                            "ttft": ttft_ms,
                            "total": total_ms,
                        },
                    )
                ).scalar_one()
            )

            for c in citations:
                await conn.execute(
                    text("""
                    INSERT INTO citations
                        (message_id, message_created_at, chunk_id, chunk_classification,
                         document_id, marker, rank, quote, page_from, page_to,
                         section_ref, verified)
                    VALUES (CAST(:mid AS uuid), :created, :chunk,
                            (SELECT classification FROM documents
                              WHERE id = CAST(:doc AS uuid)),
                            CAST(:doc AS uuid), :marker, :rank, :quote,
                            :page_from, :page_to, :section, :verified)
                    """),
                    {
                        "mid": message_id,
                        "created": now,
                        "chunk": int(c.chunk_id),
                        "doc": str(c.document_id),
                        "marker": c.marker,
                        "rank": c.rank,
                        "quote": c.quote,
                        "page_from": c.page_from,
                        "page_to": c.page_to,
                        "section": c.section_ref,
                        "verified": c.verified,
                    },
                )

            await conn.execute(
                text("""
                UPDATE conversations
                SET message_count = message_count + 2,
                    last_message_at = :now,
                    -- The first question names the conversation. Asking the user
                    -- to title it is work; deriving it costs nothing.
                    title = coalesce(title, left(:question, 80))
                WHERE id = CAST(:cid AS uuid)
                """),
                {"cid": conversation_id, "now": now, "question": question},
            )
        return message_id

    # ── feedback (FR-043, FR-044) ───────────────────────────────────────────

    async def record_feedback(
        self,
        message_id: str,
        user_id: str,
        rating: str,
        reason: str | None,
        comment: str | None,
    ) -> bool:
        async with self._engine.begin() as conn:
            owns = (
                await conn.execute(
                    text("""
                    SELECT m.created_at FROM messages m
                    JOIN conversations c ON c.id = m.conversation_id
                    WHERE m.id = CAST(:mid AS uuid)
                      AND c.user_id = CAST(:uid AS uuid)
                      AND m.role = 'assistant'
                    """),
                    {"mid": message_id, "uid": user_id},
                )
            ).scalar_one_or_none()
            if owns is None:
                return False

            await conn.execute(
                text("""
                INSERT INTO message_feedback
                    (message_id, message_created_at, user_id, rating, reason_code, comment)
                VALUES (CAST(:mid AS uuid), :created, CAST(:uid AS uuid),
                        CAST(:rating AS feedback_rating), :reason, :comment)
                ON CONFLICT (message_id, user_id) DO UPDATE
                SET rating = EXCLUDED.rating,
                    reason_code = EXCLUDED.reason_code,
                    comment = EXCLUDED.comment
                """),
                {
                    "mid": message_id,
                    "created": owns,
                    "uid": user_id,
                    "rating": rating,
                    "reason": reason,
                    "comment": comment,
                },
            )
        return True
