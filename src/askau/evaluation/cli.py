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
from askau.evaluation.history import EvalHistory
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
    parser.add_argument(
        "--no-record",
        action="store_true",
        help="do not write the run to eval_runs (for a throwaway local check)",
    )
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

    # Recorded here as well as from the API, because this is the entry point CI
    # uses — and a quality history with the pipeline's own runs missing from it
    # would have a hole exactly where the regressions are.
    #
    # The gate is computed twice: once here for the recorded verdict and once
    # below for the exit code. Cheap, and it keeps the stored `passed` from
    # depending on control flow further down.
    run_id = None
    if not args.no_record:
        recorded_pass, recorded_breaches = report.gate(THRESHOLDS)
        run_id = await EvalHistory(engine).record(
            dataset="security" if args.security else "core" if args.core else "all",
            questions=questions,
            report=report,
            passed=recorded_pass,
            breaches=recorded_breaches,
            model=f"{settings.llm_provider}:{settings.llm_model}",
            retriever=settings.retriever,
        )

    await dispose_engines()

    m = report.metrics()
    print(f"\n  questions   {report.total}")
    print(f"  passed      {report.passed}")
    print(f"  pass rate   {m['pass_rate']:.0%}" if m["pass_rate"] is not None else "")
    if m["groundedness"] is not None:
        print(
            f"  groundedness {m['groundedness']:.0%} mean"
            f" · {m['min_groundedness']:.0%} worst answer (the gated one)"
        )
    if m["retrieval_precision"] is not None:
        # Reported next to groundedness because the two answer different
        # questions and are easy to confuse: groundedness asks whether the
        # answer is supported by what was retrieved, precision asks whether what
        # was retrieved should have been. An answer can be perfectly grounded in
        # three sources when only one of them was relevant.
        print(
            f"  precision   {m['retrieval_precision']:.0%}"
            f"  (of {m['retrieval_checked']} scored; capped by top_k)"
        )
    if m["mrr"] is not None:
        # The ranking number. 1.0 = the right document was always first.
        print(f"  MRR         {m['mrr']:.2f}  (1.00 = right document always ranked first)")
    print(f"  p95 latency {m['p95_latency_ms']}ms")
    print(f"  duration    {report.duration_ms}ms")

    if report.known:
        print(f"\n  {len(report.known)} known limitation(s), excluded from the gate:")
        for r in report.known:
            print(f"    {r.question_id:12} {r.known_limitation}")

    weak = [r for r in report.results if r.groundedness is not None and r.groundedness < 1.0]
    if weak:
        print(f"\n  {len(weak)} answer(s) below full groundedness — the sentences not matched:")
        for r in weak[:5]:
            print(f"    {r.question_id:12} {r.groundedness:.0%}")
            for sentence in r.unsupported[:2]:
                print(f"                 · {sentence[:88]}")

    if report.failures:
        print(f"\n  {len(report.failures)} failing:")
        for r in report.failures:
            print(f"    {r.question_id:12} {r.failure}")
            if args.verbose:
                print(f"                 {r.question!r} -> {r.actual_state}")

    ok, breaches = report.gate(THRESHOLDS)
    print()
    if run_id:
        print(f"  recorded    {run_id}")
    elif not args.no_record:
        # Said out loud rather than passed over. Recording is non-fatal by
        # design, and a silent failure would leave a gap in the series that
        # nobody discovers until they try to compare against it.
        print("  recorded    (failed — see the log; the gate result below still stands)")
    if ok:
        print("  GATE PASSED")
        return 0
    for breach in breaches:
        print(f"  GATE FAILED: {breach}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
