"""The ingestion pipeline (FR-012 … FR-018, FR-049, FR-050).

Staged and checkpointed. Each document's stage is written to `ingestion_tasks`
as it advances, so a run that fails resumes from the failing document rather
than restarting the source — which matters when a source is thousands of files
and the failure is on the last one.

    discover -> validate -> extract -> chunk -> embed -> index -> reconcile ACLs

Content hashing makes it idempotent: an unchanged document costs one comparison,
so re-running a sync is cheap and safe.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from askau.domain.knowledge import ValidationFailure
from askau.ingestion import validation
from askau.ingestion.chunking import StructureAwareChunker
from askau.ingestion.extractors import UnsupportedFormatError, extract
from askau.ingestion.extractors import ocr as ocr_engine
from askau.ingestion.language import detect_language
from askau.llm.ports import Embedder
from askau.settings import Settings

_log = logging.getLogger(__name__)


@dataclass(slots=True)
class IngestOutcome:
    discovered: int = 0
    indexed: int = 0
    skipped: int = 0
    failed: int = 0
    chunks: int = 0
    embedding_tokens: int = 0
    failures: list[tuple[str, ValidationFailure]] = field(default_factory=list)


class IngestionPipeline:
    def __init__(self, engine: AsyncEngine, embedder: Embedder, settings: Settings) -> None:
        self._engine = engine
        self._embedder = embedder
        self._settings = settings
        self._chunker = StructureAwareChunker(
            target_tokens=settings.chunk_target_tokens,
            overlap_tokens=settings.chunk_overlap_tokens,
            max_tokens=settings.chunk_max_tokens,
        )
        #: ``None`` when OCR is off, which is the default. Built once rather
        #: than per document: the engine is a client, and a run may process
        #: thousands of files.
        self._ocr = ocr_engine.engine_for(
            settings.ocr_provider,
            settings.ocr_url,
            ocr_engine.requested_languages(settings.ocr_languages),
        )

    async def ingest_directory(
        self, root: Path, source_id: str, run_id: str, *, classification: str = "internal"
    ) -> IngestOutcome:
        outcome = IngestOutcome()
        # Filesystem walks and reads are blocking. Off the event loop, because
        # the pipeline may run inside the API process and a large directory
        # would otherwise stall every in-flight request.
        files = await asyncio.to_thread(_discover, root)
        outcome.discovered = len(files)

        for path in files:
            task_id = await self._open_task(run_id, path.name)
            try:
                await self._one(path, source_id, task_id, classification, outcome)
            except Exception as exc:
                outcome.failed += 1
                failure = ValidationFailure(
                    code="extraction_error",
                    message=f"{type(exc).__name__}: {exc}",
                    remedy="Check the file is not corrupt, then reprocess it.",
                )
                outcome.failures.append((path.name, failure))
                await self._fail_task(task_id, failure)
                _log.exception("ingestion failed for %s", path.name)

        await self._close_run(run_id, outcome)
        return outcome

    # ── stages ──────────────────────────────────────────────────────────────

    async def _one(
        self,
        path: Path,
        source_id: str,
        task_id: int,
        classification: str,
        outcome: IngestOutcome,
    ) -> None:
        data = await asyncio.to_thread(path.read_bytes)

        checked = validation.validate_file(path.name, data)
        if isinstance(checked, ValidationFailure):
            outcome.failed += 1
            outcome.failures.append((path.name, checked))
            await self._fail_task(task_id, checked)
            return

        if await self._unchanged(source_id, path.name, checked.content_hash):
            outcome.skipped += 1
            await self._set_stage(task_id, "skipped_unchanged")
            return

        await self._set_stage(task_id, "extracting")
        try:
            extraction = extract(data, path.name)
        except UnsupportedFormatError as exc:
            failure = ValidationFailure(
                code="unsupported_format",
                message=str(exc),
                remedy="Convert the document, or exclude it with the source owner.",
            )
            outcome.failed += 1
            outcome.failures.append((path.name, failure))
            await self._fail_task(task_id, failure)
            return

        problem = validation.validate_extraction(path.name, extraction)

        # A scan reaches here with no text. OCR is the second attempt — and only
        # for `no_text_layer`, never for `insufficient_text`: a cover sheet that
        # genuinely says little would be re-read at great cost to confirm it
        # still says little, and OCR of a page that has real text replaces
        # something accurate with a guess.
        if problem is not None and problem.code == "no_text_layer" and self._ocr is not None:
            await self._set_stage(task_id, "ocr")
            try:
                extraction = await ocr_engine.extract_pdf(self._ocr, data, path.name)
                problem = validation.validate_extraction(path.name, extraction)
            except ocr_engine.OcrUnavailableError as exc:
                # Distinguished from "this document is a scan": one is an
                # infrastructure fault the platform team fixes, the other is a
                # document the source owner must replace. Reporting the former
                # as the latter sends people to argue with the wrong party.
                _log.warning("%s: OCR failed: %s", path.name, exc)
                problem = ValidationFailure(
                    code="ocr_unavailable",
                    message=f"{path.name} needs OCR but the OCR service failed",
                    remedy=(
                        "This is an infrastructure fault, not a document problem. "
                        "Check /health/deep — the OCR service is unreachable or erroring."
                    ),
                )

        if problem is not None:
            outcome.failed += 1
            outcome.failures.append((path.name, problem))
            await self._fail_task(task_id, problem)
            return

        await self._set_stage(task_id, "chunking")
        chunks = self._chunker.chunk(extraction.blocks)
        if not chunks:
            failure = ValidationFailure(
                code="no_chunks",
                message="Extraction produced text but chunking produced nothing",
                remedy="Report this — it is a defect rather than a document problem.",
            )
            outcome.failed += 1
            outcome.failures.append((path.name, failure))
            await self._fail_task(task_id, failure)
            return

        language = detect_language(extraction.text)
        risk = validation.injection_risk(extraction)

        await self._set_stage(task_id, "embedding")
        vectors = await self._embedder.embed_documents([c.content for c in chunks])
        outcome.embedding_tokens += sum(c.token_count for c in chunks)

        # No "indexing" stage: the enum goes embedding -> indexed, and the write
        # below is what moves it. Inventing a value the type does not have is
        # exactly the class of error a database enum exists to catch.
        document_id = await self._write(
            path,
            source_id,
            checked,
            extraction,
            chunks,
            vectors,
            language.language,
            language.ts_config,
            classification,
            risk,
        )
        await self._link_task(task_id, document_id)

        outcome.indexed += 1
        outcome.chunks += len(chunks)

    # ── persistence ─────────────────────────────────────────────────────────

    async def _unchanged(self, source_id: str, key: str, digest: bytes) -> bool:
        async with self._engine.connect() as conn:
            return bool(
                (
                    await conn.execute(
                        text("""
                        SELECT 1 FROM documents d
                        JOIN document_families f ON f.id = d.family_id
                        WHERE f.source_id = CAST(:sid AS uuid)
                          AND f.external_key = :key
                          AND d.content_hash = :hash
                          AND d.ingest_status = 'indexed'
                        """),
                        {"sid": source_id, "key": key, "hash": digest},
                    )
                ).scalar_one_or_none()
            )

    async def _write(
        self,
        path: Path,
        source_id: str,
        checked: validation.ValidatedDocument,
        extraction: object,
        chunks: object,
        vectors: object,
        language: str,
        ts_config: str,
        classification: str,
        risk: int,
    ) -> str:
        from askau.domain.knowledge import ExtractionResult, PendingChunk

        assert isinstance(extraction, ExtractionResult)
        chunk_list: list[PendingChunk] = list(chunks)  # type: ignore[call-overload]
        vector_list: list[list[float]] = list(vectors)  # type: ignore[call-overload]

        async with self._engine.begin() as conn:
            family_id = (
                await conn.execute(
                    text("""
                    INSERT INTO document_families (source_id, external_key, canonical_title)
                    VALUES (CAST(:sid AS uuid), :key, :title)
                    ON CONFLICT (source_id, external_key) DO UPDATE
                      SET canonical_title = EXCLUDED.canonical_title
                    RETURNING id
                    """),
                    {"sid": source_id, "key": path.name, "title": path.stem},
                )
            ).scalar_one()

            document_id = str(
                (
                    await conn.execute(
                        text("""
                        INSERT INTO documents
                            (family_id, source_id, title, language, source_uri, mime_type,
                             byte_size, page_count, content_hash, classification,
                             lifecycle, ingest_status, injection_risk, review_required,
                             indexed_at, last_synced_at, chunk_count)
                        VALUES (:fam, CAST(:sid AS uuid), :title, :lang, :uri, :mime,
                                :size, :pages, :hash, CAST(:cls AS classification),
                                'active', 'indexed', :risk, :review, now(), now(), :n)
                        RETURNING id
                        """),
                        {
                            "fam": family_id,
                            "sid": source_id,
                            "title": path.stem,
                            "lang": language,
                            "uri": path.as_uri(),
                            "mime": _mime_for(checked.suffix),
                            "size": checked.byte_size,
                            "pages": extraction.page_count,
                            "hash": checked.content_hash,
                            "cls": classification,
                            "risk": risk,
                            # FR-036: a high score routes to review, never blocks.
                            "review": risk >= 50,
                            "n": len(chunk_list),
                        },
                    )
                ).scalar_one()
            )
            await conn.execute(
                text(
                    "UPDATE document_families SET current_document_id = CAST(:d AS uuid) "
                    "WHERE id = :f"
                ),
                {"d": document_id, "f": family_id},
            )
            await conn.execute(
                text("""
                INSERT INTO document_acl (document_id, principal_id)
                SELECT CAST(:d AS uuid), a.principal_id
                FROM knowledge_sources ks
                CROSS JOIN LATERAL (
                    SELECT p.id AS principal_id FROM principals p
                    WHERE p.external_id = ANY(
                        SELECT jsonb_array_elements_text(
                            coalesce(ks.access_rules->'principals', '["grp-all-staff"]'::jsonb))
                    )
                ) a
                WHERE ks.id = CAST(:sid AS uuid)
                ON CONFLICT DO NOTHING
                """),
                {"d": document_id, "sid": source_id},
            )

            for chunk, vector in zip(chunk_list, vector_list, strict=True):
                await conn.execute(
                    text("""
                    INSERT INTO chunks
                        (document_id, family_id, ordinal, content, token_count,
                         heading_path, section_ref, page_from, page_to,
                         char_start, char_end, classification, lifecycle, language,
                         version_seq, is_current, acl_principals, embedding,
                         lang_config, tsv)
                    VALUES (CAST(:doc AS uuid), :fam, :ord, :content, :tokens,
                            CAST(:heads AS text[]), :section, :pfrom, :pto,
                            :cstart, :cend, CAST(:cls AS classification), 'active', :lang,
                            1, TRUE,
                            (SELECT coalesce(array_agg(principal_id), '{}')
                             FROM document_acl WHERE document_id = CAST(:doc AS uuid)),
                            CAST(:emb AS vector), CAST(:tscfg AS regconfig),
                            to_tsvector(CAST(:tscfg AS regconfig), :content))
                    """),
                    {
                        "doc": document_id,
                        "fam": family_id,
                        "ord": chunk.ordinal,
                        "content": chunk.content,
                        "tokens": chunk.token_count,
                        "heads": list(chunk.heading_path),
                        "section": chunk.section_ref,
                        "pfrom": chunk.page_from,
                        "pto": chunk.page_to,
                        "cstart": chunk.char_start,
                        "cend": chunk.char_end,
                        "cls": classification,
                        "lang": language,
                        "emb": "[" + ",".join(f"{v:.6f}" for v in vector) + "]",
                        "tscfg": ts_config,
                    },
                )
        return document_id

    async def _open_task(self, run_id: str, key: str) -> int:
        async with self._engine.begin() as conn:
            return int(
                (
                    await conn.execute(
                        text("""
                        INSERT INTO ingestion_tasks (run_id, external_key, stage)
                        VALUES (CAST(:r AS uuid), :k, 'fetching') RETURNING id
                        """),
                        {"r": run_id, "k": key},
                    )
                ).scalar_one()
            )

    async def _set_stage(self, task_id: int, stage: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text("""
                UPDATE ingestion_tasks
                SET stage = CAST(:s AS ingest_status), updated_at = now()
                WHERE id = :id
                """),
                {"id": task_id, "s": stage},
            )

    async def _link_task(self, task_id: int, document_id: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text("""
                UPDATE ingestion_tasks
                SET stage = 'indexed', document_id = CAST(:d AS uuid), updated_at = now()
                WHERE id = :id
                """),
                {"id": task_id, "d": document_id},
            )

    async def _fail_task(self, task_id: int, failure: ValidationFailure) -> None:
        import json

        async with self._engine.begin() as conn:
            await conn.execute(
                text("""
                UPDATE ingestion_tasks
                SET stage = 'failed', error_code = :code, error_detail = CAST(:d AS jsonb),
                    attempts = attempts + 1, updated_at = now()
                WHERE id = :id
                """),
                {
                    "id": task_id,
                    "code": failure.code,
                    "d": json.dumps({"message": failure.message, "remedy": failure.remedy}),
                },
            )

    async def _close_run(self, run_id: str, outcome: IngestOutcome) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text("""
                UPDATE ingestion_runs
                SET status = :status, docs_discovered = :disc, docs_processed = :ok,
                    docs_skipped = :skip, docs_failed = :bad, chunks_written = :chunks,
                    embedding_tokens = :tokens, finished_at = now()
                WHERE id = CAST(:r AS uuid)
                """),
                {
                    "r": run_id,
                    "status": "completed" if not outcome.failed else "partial",
                    "disc": outcome.discovered,
                    "ok": outcome.indexed,
                    "skip": outcome.skipped,
                    "bad": outcome.failed,
                    "chunks": outcome.chunks,
                    "tokens": outcome.embedding_tokens,
                },
            )


def _discover(root: Path) -> list[Path]:
    supported = _supported()
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in supported)


def _supported() -> frozenset[str]:
    from askau.ingestion.extractors import SUPPORTED_SUFFIXES

    return SUPPORTED_SUFFIXES


def _mime_for(suffix: str) -> str:
    return {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".html": "text/html",
        ".htm": "text/html",
        ".txt": "text/plain",
        ".md": "text/markdown",
    }.get(suffix, "application/octet-stream")
