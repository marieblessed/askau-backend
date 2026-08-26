"""Prometheus exposition (NFR-009).

Rendered from the database rather than from in-process counters, deliberately:
the application is horizontally scaled and stateless, so per-process counters
would report one replica's view. The figures that matter here — answers,
refusals, latency — are already durable in `messages`.

Cluster-internal only. It is unauthenticated because a scrape target that needs
a bearer token is a scrape target nobody configures correctly, and it exposes
counts and latencies, never content.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

_QUERY = text("""
SELECT
  count(*) FILTER (WHERE role = 'assistant')                    AS answers,
  count(*) FILTER (WHERE answer_state = 'grounded')             AS grounded,
  count(*) FILTER (WHERE answer_state IN
      ('insufficient_evidence','out_of_scope','refused_safety')) AS refused,
  count(*) FILTER (WHERE answer_state = 'conflict')             AS conflicts,
  count(*) FILTER (WHERE answer_state = 'error')                AS errors,
  coalesce(percentile_disc(0.95) WITHIN GROUP (ORDER BY ttft_ms), 0)  AS ttft_p95,
  coalesce(percentile_disc(0.95) WITHIN GROUP (ORDER BY total_ms), 0) AS total_p95,
  coalesce(avg(groundedness), 0)                                AS groundedness
FROM messages
WHERE created_at > now() - interval '1 hour'
""")

_CORPUS = text("""
SELECT (SELECT count(*) FROM documents)                              AS documents,
       (SELECT count(*) FROM documents WHERE ingest_status='failed') AS failed,
       (SELECT coalesce(sum(chunk_count),0) FROM documents)          AS chunks,
       (SELECT count(*) FROM audit_events
          WHERE outcome='denied' AND occurred_at > now() - interval '1 hour')
                                                                     AS denials
""")


async def render(engine: AsyncEngine) -> str:
    async with engine.connect() as conn:
        m = (await conn.execute(_QUERY)).mappings().one()
        c = (await conn.execute(_CORPUS)).mappings().one()

    lines = [
        "# HELP askau_answers_total Answers produced in the last hour, by state.",
        "# TYPE askau_answers_total counter",
        f'askau_answers_total{{state="grounded"}} {m["grounded"]}',
        f'askau_answers_total{{state="refused"}} {m["refused"]}',
        f'askau_answers_total{{state="conflict"}} {m["conflicts"]}',
        f'askau_answers_total{{state="error"}} {m["errors"]}',
        "# HELP askau_answer_latency_ms Answer latency, 95th percentile.",
        "# TYPE askau_answer_latency_ms gauge",
        f'askau_answer_latency_ms{{phase="first_token"}} {m["ttft_p95"]}',
        f'askau_answer_latency_ms{{phase="complete"}} {m["total_p95"]}',
        "# HELP askau_groundedness Mean groundedness over the last hour.",
        "# TYPE askau_groundedness gauge",
        f"askau_groundedness {float(m['groundedness']):.4f}",
        "# HELP askau_corpus_documents Documents in the knowledge base.",
        "# TYPE askau_corpus_documents gauge",
        f"askau_corpus_documents {c['documents']}",
        f"askau_corpus_chunks {c['chunks']}",
        "# HELP askau_ingestion_failed_documents Documents whose ingestion failed.",
        "# TYPE askau_ingestion_failed_documents gauge",
        f"askau_ingestion_failed_documents {c['failed']}",
        # The one with a zero target. Alerting on this pages immediately.
        "# HELP askau_access_denials_total Unauthorized access attempts, last hour.",
        "# TYPE askau_access_denials_total counter",
        f"askau_access_denials_total {c['denials']}",
    ]
    return "\n".join(lines) + "\n"
