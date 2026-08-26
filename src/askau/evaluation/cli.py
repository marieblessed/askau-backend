"""Evaluation CLI — the build gate.

    python -m askau.evaluation.cli            # everything
    python -m askau.evaluation.cli --security # the zero-tolerance suite only

Exits non-zero when the gate fails, so CI can call it directly.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from askau.db.engine import dispose_engines, get_engine
from askau.evaluation.datasets import ALL, CORE, SECURITY, THRESHOLDS
from askau.evaluation.runner import EvaluationRunner
from askau.llm.registry import build_embedder, build_llm
from askau.rag.orchestrator import RagOrchestrator
from askau.retrieval.adapters.pgvector_hybrid import PgVectorHybridRetriever
from askau.retrieval.adapters.rerank_noop import NoopReranker
from askau.settings import get_settings


async def main() -> int:
    parser = argparse.ArgumentParser(description="AskAU evaluation gate")
    parser.add_argument("--security", action="store_true", help="security suite only")
    parser.add_argument("--core", action="store_true", help="core suite only")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    questions = SECURITY if args.security else CORE if args.core else ALL
    settings = get_settings()
    engine = get_engine(settings)
    orchestrator = RagOrchestrator(
        retriever=PgVectorHybridRetriever(engine, settings),
        embedder=build_embedder(settings),
        llm=build_llm(settings),
        settings=settings,
        reranker=NoopReranker(),
    )

    report = await EvaluationRunner(orchestrator, engine).run(questions)
    await dispose_engines()

    m = report.metrics()
    print(f"\n  questions   {report.total}")
    print(f"  passed      {report.passed}")
    print(f"  pass rate   {m['pass_rate']:.0%}" if m["pass_rate"] is not None else "")
    if m["groundedness"] is not None:
        print(f"  groundedness {m['groundedness']:.0%}")
    print(f"  p95 latency {m['p95_latency_ms']}ms")
    print(f"  duration    {report.duration_ms}ms")

    if report.known:
        print(f"\n  {len(report.known)} known limitation(s), excluded from the gate:")
        for r in report.known:
            print(f"    {r.question_id:12} {r.known_limitation}")

    if report.failures:
        print(f"\n  {len(report.failures)} failing:")
        for r in report.failures:
            print(f"    {r.question_id:12} {r.failure}")
            if args.verbose:
                print(f"                 {r.question!r} -> {r.actual_state}")

    ok, breaches = report.gate(THRESHOLDS)
    print()
    if ok:
        print("  GATE PASSED")
        return 0
    for breach in breaches:
        print(f"  GATE FAILED: {breach}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
