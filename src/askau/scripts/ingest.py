"""Run one ingestion pass for a knowledge source.

`POST /knowledge/sources/{id}/sync` opens a row in `ingestion_runs` with status
`running`, returns `202` with the run id, and stops. Nothing then picks the run
up: `make worker` names `askau.workers.main`, which does not exist, and
`IngestionPipeline.ingest_connector` is called by tests and by nothing else.

So a triggered sync sat at `running` forever and the corpus never changed —
visible only as a run that never finishes, which reads as a slow job rather than
an absent one. This is the missing half.

It is not a scheduler and does not poll a queue. One source, one pass, on
demand: enough to ingest a blob container or re-walk a directory and see the
result, which is what Phase 1 needs and what a real deployment will replace with
whatever it uses to run periodic work.

**Runs as the application role, deliberately.** `askau_app` holds INSERT on
`chunks` (migration 0009) and the write policies that make row-level security
allow it (0010). Ingesting as the migration superuser would work and would stop
exercising exactly the grants a deployment depends on — 0009 exists because the
seed ran elevated and hid a missing privilege until the first real ingest.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from askau.ingestion.connectors import build, probe
from askau.ingestion.pipeline import IngestionPipeline
from askau.llm.registry import build_embedder
from askau.settings import get_settings


def _parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="ingest",
        description="Run one ingestion pass for a knowledge source.",
    )
    p.add_argument("--source", required=True, help="knowledge_sources.id")
    p.add_argument(
        "--run",
        help=(
            "Adopt an existing ingestion_runs row instead of opening one — "
            "the id returned by POST /knowledge/sources/{id}/sync."
        ),
    )
    p.add_argument(
        "--classification",
        help=(
            "Override the classification applied to documents this run. "
            "Defaults to the source's own default_classification."
        ),
    )
    return p.parse_args(argv)


async def run(argv: list[str]) -> int:
    args = _parse(argv)

    # Validated here rather than by the driver. Handing a non-UUID to asyncpg
    # produces eighty lines of protocol traceback for what is a typo, and the
    # useful sentence is nowhere near the top of it.
    for label, value in (("--source", args.source), ("--run", args.run)):
        if value is None:
            continue
        try:
            uuid.UUID(value)
        except ValueError:
            print(f"  {label} is not a uuid: {value!r}", file=sys.stderr)
            return 2

    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text("""
                    SELECT name, source_type, location, status, default_classification::text
                    FROM knowledge_sources WHERE id = CAST(:s AS uuid)
                    """),
                    {"s": args.source},
                )
            ).first()

        if row is None:
            print(f"  no knowledge source with id {args.source}", file=sys.stderr)
            return 2

        name, source_type, location, status, source_classification = row

        # The source's own default, not a constant in this file.
        #
        # It was `--classification` defaulting to "internal", which silently
        # overrode a source declaring itself `public` — every document from the
        # AU's public library landed marked INTERNAL. Wrong in the direction
        # that looks harmless: over-classifying hides public material behind a
        # clearance check rather than exposing anything, but it is still the
        # corpus lying about its own documents.
        classification = args.classification or source_classification
        print(f"  source   {name} ({source_type}, {status}, {classification})")

        # Reported rather than refused. A paused source is a legitimate thing to
        # ingest deliberately — an administrator checking a fix before
        # reactivating it — and refusing would make the tool useless for the one
        # case where a manual pass is most wanted.
        if status != "active":
            print(f"  note: source status is '{status}', not 'active'")

        loc = dict(location or {})

        # `filesystem` is not a connector — it has no per-item access control,
        # so it goes through `ingest_directory` and inherits the source's
        # `access_rules`. Everything else goes through the connector port.
        # Dispatching here rather than teaching `build` to return a fake
        # connector: the difference between the two paths is exactly the
        # difference `IngestItem.principals` encodes, and blurring it is how a
        # document with no determinable audience becomes readable by everyone.
        connector = None
        if source_type != "filesystem":
            connector = build(source_type, loc, settings)
            if connector is None:
                print(
                    f"  no connector for source_type '{source_type}'. For Azure Blob "
                    "Storage, set ASKAU_AZURE_STORAGE_CLIENT_ID and "
                    "ASKAU_AZURE_STORAGE_CLIENT_SECRET (or ASKAU_AZURE_STORAGE_SAS_TOKEN).",
                    file=sys.stderr,
                )
                return 2

        # Probed before the run row is opened, so a source that cannot be
        # reached does not leave a failed run behind to explain.
        verdict = await probe(source_type, loc, settings)
        if not verdict.get("ok"):
            print(f"  unreachable: {verdict.get('detail')}", file=sys.stderr)
            if connector is not None:
                await connector.aclose()
            return 2
        print(f"  reachable: {verdict.get('detail', 'ok')}")

        if args.run:
            run_id = args.run
            async with engine.begin() as conn:
                claimed = (
                    await conn.execute(
                        text("""
                        UPDATE ingestion_runs SET status = 'running'
                        WHERE id = CAST(:r AS uuid) AND source_id = CAST(:s AS uuid)
                          AND finished_at IS NULL
                        RETURNING id
                        """),
                        {"r": run_id, "s": args.source},
                    )
                ).scalar_one_or_none()
            if claimed is None:
                print(
                    f"  run {run_id} does not belong to this source, or has already "
                    "finished. Refusing to rewrite a completed run.",
                    file=sys.stderr,
                )
                if connector is not None:
                    await connector.aclose()
                return 2
        else:
            async with engine.begin() as conn:
                run_id = str(
                    (
                        await conn.execute(
                            text("""
                            INSERT INTO ingestion_runs (source_id, trigger, status)
                            VALUES (CAST(:s AS uuid), 'manual', 'running')
                            RETURNING id
                            """),
                            {"s": args.source},
                        )
                    ).scalar_one()
                )
        print(f"  run      {run_id}")

        pipeline = IngestionPipeline(engine, build_embedder(settings), settings)
        try:
            if connector is None:
                # `expanduser` reads the environment and the passwd database,
                # so it is a filesystem call like any other and does not belong
                # on the event loop.
                root = await asyncio.to_thread(lambda: Path(str(loc["path"])).expanduser())
                outcome = await pipeline.ingest_directory(
                    root,
                    args.source,
                    run_id,
                    classification=classification,
                )
            else:
                outcome = await pipeline.ingest_connector(
                    connector, args.source, run_id, classification=classification
                )
        finally:
            if connector is not None:
                await connector.aclose()

        print(
            f"  discovered {outcome.discovered}, indexed {outcome.indexed}, "
            f"skipped {outcome.skipped}, failed {outcome.failed}, expired {outcome.expired}"
        )
        print(f"  chunks {outcome.chunks}, embedding tokens {outcome.embedding_tokens}")

        for filename, failure in outcome.failures:
            print(f"  ! {filename}: {failure.message}")

        # Non-zero on any failure. A run that could not assess a document's
        # permissions is not a success with a footnote — that document is
        # absent from the corpus, and a caller scripting this needs to know.
        return 1 if outcome.failed else 0
    finally:
        await engine.dispose()


def main(argv: list[str]) -> int:
    return asyncio.run(run(argv))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
