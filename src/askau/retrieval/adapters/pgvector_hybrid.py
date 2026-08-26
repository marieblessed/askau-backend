"""Hybrid retrieval: one SQL statement, ACL-filtered, RRF-fused in the database.

This is the authorization boundary. Two properties matter more than anything
else in this file:

1. **The ACL predicate appears in BOTH arms.** A filter on the semantic arm
   alone is a leak on the keyword arm. They are written as one shared fragment
   so they cannot drift apart.
2. **Metadata joins happen after LIMIT.** Forty index lookups, not a join across
   fifty million rows.

Everything else — RRF, partition pruning, the version predicate — is here to
make that one query fast enough to be the only one on the path.
"""

from __future__ import annotations

import time

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from askau.domain.enums import Classification, Lifecycle, RetrievalStrategy
from askau.domain.retrieval import (
    ChunkId,
    DocumentId,
    RetrievalQuery,
    RetrievalResult,
    RetrievedChunk,
)
from askau.retrieval.policy import VersionPolicy
from askau.settings import Settings

#: The authorization predicate. Defined once and interpolated into both arms —
#: if it were written twice, one copy could be edited without the other.
_ACL = "c.acl_principals && CAST(:principals AS bigint[])"

_HYBRID_SQL = """
WITH semantic AS (
    SELECT c.id, c.classification,
           row_number() OVER (ORDER BY c.embedding <=> CAST(:qvec AS vector)) AS rnk
    FROM chunks c
    WHERE {acl}
      AND {version}
      AND c.embedding IS NOT NULL
      {dept}
    ORDER BY c.embedding <=> CAST(:qvec AS vector)
    LIMIT :candidate_k
),
keyword AS (
    SELECT c.id, c.classification,
           row_number() OVER (
               ORDER BY ts_rank_cd(c.tsv, websearch_to_tsquery(:ts_config, :qtext)) DESC
           ) AS rnk
    FROM chunks c
    WHERE {acl}
      AND {version}
      AND c.tsv @@ websearch_to_tsquery(:ts_config, :qtext)
      {dept}
    ORDER BY ts_rank_cd(c.tsv, websearch_to_tsquery(:ts_config, :qtext)) DESC
    LIMIT :candidate_k
),
fused AS (
    SELECT id, classification,
           SUM(1.0 / (:rrf_k + rnk))                       AS rrf_score,
           MIN(rnk) FILTER (WHERE arm = 's')               AS semantic_rank,
           MIN(rnk) FILTER (WHERE arm = 'k')               AS keyword_rank
    FROM (
        SELECT id, classification, rnk, 's' AS arm FROM semantic
        UNION ALL
        SELECT id, classification, rnk, 'k' AS arm FROM keyword
    ) arms
    GROUP BY id, classification
    ORDER BY rrf_score DESC
    LIMIT :rerank_k
)
SELECT f.rrf_score, f.semantic_rank, f.keyword_rank,
       c.id, c.content, c.heading_path, c.section_ref,
       c.page_from, c.page_to, c.token_count, c.version_seq,
       c.classification, c.lifecycle, c.language,
       d.id AS document_id, d.title, d.source_uri,
       d.version_label, d.effective_from, d.effective_to,
       ks.name AS source_name
FROM fused f
JOIN chunks c            ON c.id = f.id AND c.classification = f.classification
JOIN documents d         ON d.id = c.document_id
JOIN knowledge_sources ks ON ks.id = d.source_id
ORDER BY f.rrf_score DESC
"""


class PgVectorHybridRetriever:
    """The default retriever. Satisfies the ``Retriever`` protocol structurally."""

    def __init__(self, engine: AsyncEngine, settings: Settings) -> None:
        self._engine = engine
        self._settings = settings

    async def search(self, query: RetrievalQuery) -> RetrievalResult:
        started = time.perf_counter()
        policy = VersionPolicy(
            include_historical=query.include_historical,
            # Asking for history and then filtering by today's date would return
            # nothing: an expired document is by definition outside its window.
            respect_effective_dates=not query.include_historical,
        )

        dept_clause = ""
        params: dict[str, object] = {
            "qvec": _vector_literal(query.embedding),
            "qtext": query.text,
            "ts_config": query.ts_config,
            "principals": query.authz.principal_array(),
            "candidate_k": query.candidate_k,
            "rerank_k": max(query.top_k, self._settings.rerank_input_k),
            "rrf_k": self._settings.rrf_k,
        }
        if query.department_filter:
            dept_clause = "AND c.department = ANY(CAST(:departments AS text[]))"
            params["departments"] = list(query.department_filter)

        sql = _HYBRID_SQL.format(acl=_ACL, version=policy.sql_predicate(), dept=dept_clause)

        async with self._engine.begin() as conn:
            await self._prepare_session(conn, query)
            rows = (await conn.execute(text(sql), params)).mappings().all()

        chunks = tuple(_to_chunk(r) for r in rows)
        return RetrievalResult(
            chunks=chunks,
            strategy=RetrievalStrategy.HYBRID,
            candidates_considered=len(rows),
            took_ms=int((time.perf_counter() - started) * 1000),
            diagnostics={
                "ts_config": query.ts_config,
                "principals": len(query.authz.principals),
                "both_arms": sum(1 for c in chunks if c.matched_both_arms),
            },
        )

    async def explain(self, query: RetrievalQuery) -> str:
        """Return the query plan. Used by the integration suite to assert that
        partition pruning and the GIN index are actually being used, rather than
        assuming the planner agrees with the design."""
        policy = VersionPolicy(
            include_historical=query.include_historical,
            # Asking for history and then filtering by today's date would return
            # nothing: an expired document is by definition outside its window.
            respect_effective_dates=not query.include_historical,
        )
        sql = _HYBRID_SQL.format(acl=_ACL, version=policy.sql_predicate(), dept="")
        params = {
            "qvec": _vector_literal(query.embedding),
            "qtext": query.text,
            "ts_config": query.ts_config,
            "principals": query.authz.principal_array(),
            "candidate_k": query.candidate_k,
            "rerank_k": self._settings.rerank_input_k,
            "rrf_k": self._settings.rrf_k,
        }
        async with self._engine.begin() as conn:
            await self._prepare_session(conn, query)
            rows = (await conn.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {sql}"), params)).all()
        return "\n".join(str(r[0]) for r in rows)

    async def _prepare_session(self, conn: AsyncConnection, query: RetrievalQuery) -> None:
        """Per-transaction settings the query depends on.

        ``askau.principals`` is what the row-level security policy reads. The
        application also passes the ACL predicate explicitly, so this is the
        *second* lock rather than the first — but both must be armed, and setting
        it here means no retrieval path can run without it.

        ``SET LOCAL`` scopes all three to the transaction, so a pooled connection
        cannot carry one caller's principals into another caller's query.
        """
        await conn.execute(
            text("SELECT set_config('askau.principals', :principals, true)"),
            {"principals": "{" + ",".join(str(p) for p in query.authz.principal_array()) + "}"},
        )
        # Not optional: below pgvector 0.8 a selective ACL filter makes HNSW
        # return fewer than k rows, with no error at all.
        await conn.execute(
            text(f"SET LOCAL hnsw.iterative_scan = '{self._settings.hnsw_iterative_scan}'")
        )
        await conn.execute(
            text(f"SET LOCAL hnsw.max_scan_tuples = {self._settings.hnsw_max_scan_tuples}")
        )


def _vector_literal(embedding: list[float]) -> str:
    """pgvector accepts a bracketed literal; this avoids a driver-level codec."""
    return "[" + ",".join(f"{v:.7g}" for v in embedding) + "]"


def _to_chunk(r: RowMapping) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=ChunkId(r["id"]),
        document_id=DocumentId(str(r["document_id"])),
        content=r["content"],
        score=float(r["rrf_score"]),
        document_title=r["title"],
        source_uri=r["source_uri"],
        source_name=r["source_name"],
        heading_path=tuple(r["heading_path"] or ()),
        section_ref=r["section_ref"],
        page_from=r["page_from"],
        page_to=r["page_to"],
        classification=Classification(r["classification"]),
        lifecycle=Lifecycle(r["lifecycle"]),
        version_label=r["version_label"],
        version_seq=r["version_seq"],
        effective_from=r["effective_from"],
        effective_to=r["effective_to"],
        language=r["language"],
        token_count=r["token_count"],
        semantic_rank=r["semantic_rank"],
        keyword_rank=r["keyword_rank"],
    )
