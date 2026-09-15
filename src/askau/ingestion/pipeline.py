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
import json
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from askau.domain.enums import Classification
from askau.domain.knowledge import ValidationFailure
from askau.ingestion import validation
from askau.ingestion.acl_reconciler import AclReconciler
from askau.ingestion.chunking import StructureAwareChunker
from askau.ingestion.connectors.ports import (
    AccessUnavailableError,
    SourceConnector,
    SourcePrincipal,
)
from askau.ingestion.extractors import UnsupportedFormatError, extract
from askau.ingestion.extractors import ocr as ocr_engine
from askau.ingestion.language import detect_language
from askau.llm.ports import Embedder
from askau.settings import Settings

_log = logging.getLogger(__name__)

#: The lifecycle every ingested document is given, and the Phase 1 assumption
#: behind it: **everything in an approved source is already approved.**
#:
#: BR-001 approves the *source* — a knowledge administrator says once that a
#: Azure Blob container may be indexed. It says nothing about the individual
#: documents inside it, and nothing here currently distinguishes them: a draft
#: saved into an approved library is indexed as current policy and can be quoted
#: as such. That is accepted for Phase 1 by decision, not by oversight.
#:
#: What replaces it: a source repository may maintain a moderation status per item
#: (Draft / Pending / Approved / Rejected) when content approval is enabled on a
#: library. Mapping that onto `lifecycle` is the intended fix, because the
#: document owners already maintain it — a second approval queue inside AskAU is
#: a process nobody would follow, and an approval nobody performs is worse than
#: none because it still looks like a control.
#:
#: Retrieval already honours this column (`retrieval/policy.py` admits only
#: `active` and `review_required`, in both search arms), so closing the gap is a
#: change to what is written here and nowhere else.
#:
#: `test_phase1_assumptions.py` fails if this changes, so the decision cannot be
#: quietly reversed without the documentation being updated with it.
PHASE1_ASSUMED_LIFECYCLE = "active"


@dataclass(frozen=True, slots=True)
class IngestItem:
    """One document, however it was discovered.

    The pipeline used to take a `Path` and derive everything from it — the key
    from `.name`, the title from `.stem`, the URI from `.as_uri()`. That works
    for a filesystem export and for nothing else, so this is the shape both a
    local directory and a remote library can produce.

    `principals` is the field that matters. `None` means "this source has no
    per-item access control", which is true of a filesystem export and makes
    the document inherit the source's `access_rules`. A tuple — **including an
    empty one** — means the connector determined the audience itself, and the
    source's rules must not be consulted. Those two cases must never collapse
    into one: an empty tuple read as "no answer" would publish a document with
    no grantees to everybody who can reach its library.
    """

    key: str
    title: str
    uri: str
    filename: str
    data: bytes
    #: The family this revision belongs to; `None` means the key is the family.
    family_key: str | None = None
    principals: tuple[SourcePrincipal, ...] | None = None
    #: The document's own classification, or `None` to take the run's default.
    #: See `RemoteDocument.classification` for why this is per document.
    classification: str | None = None
    #: Governance metadata from the source; `None` where it said nothing.
    doc_type: str | None = None
    department: str | None = None
    version_label: str | None = None
    effective_from: str | None = None
    effective_to: str | None = None


def _as_date(value: str | None) -> date | None:
    """ISO date string to `date`, or `None`.

    Returns `None` on an unparseable value rather than raising: the connector
    has already refused those, so reaching here with one is a bug in a
    connector, and aborting a whole run over it would be a worse outcome than
    an absent date on one document.
    """
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        _log.warning("unparseable date %r reached the pipeline; storing NULL", value)
        return None


#: Values the `classification` enum accepts. Derived from the enum rather than
#: retyped, so a new tier cannot be added to the schema and forgotten here.
_CLASSIFICATIONS = frozenset(c.value for c in Classification)


@dataclass(slots=True)
class IngestOutcome:
    discovered: int = 0
    indexed: int = 0
    skipped: int = 0
    failed: int = 0
    #: Documents the source no longer has, marked out of default retrieval.
    expired: int = 0
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
        """Ingest a local export.

        Expiry applies here for the same reason it does to a remote library, and
        it was an oversight that it did not: a file removed from an export is a
        document withdrawn, and leaving it in the corpus means a rescinded
        policy keeps being cited as current. The filesystem path is what runs
        today, so the gap was live rather than theoretical.

        The safety conditions are identical and carried by `_expire_absent`: a
        walk that raised expires nothing, and an empty directory expires nothing.
        """
        outcome = IngestOutcome()
        # Filesystem walks and reads are blocking. Off the event loop, because
        # the pipeline may run inside the API process and a large directory
        # would otherwise stall every in-flight request.
        try:
            files = await asyncio.to_thread(_discover, root)
        except Exception as exc:
            # An unreadable root is the filesystem equivalent of an enumeration
            # that died, and it must close the run for the same reason: a row
            # left `running` refuses every future sync of this source.
            await self._fail_run(run_id, outcome, exc)
            raise
        outcome.discovered = len(files)

        for path in files:
            data = await asyncio.to_thread(path.read_bytes)
            item = IngestItem(
                key=path.name,
                title=path.stem,
                uri=path.as_uri(),
                filename=path.name,
                data=data,
                # None, not (): a filesystem export carries no per-file
                # permissions, so the source's rules are the only answer there
                # is. See `IngestItem.principals`.
                principals=None,
            )
            await self._ingest_one(item, source_id, run_id, classification, outcome)

        # `_discover` returned, so the walk completed — the condition
        # `_expire_absent` requires. A partial walk raised above and never
        # reaches here.
        outcome.expired = await self._expire_absent(
            source_id, {p.name for p in files}, complete=True
        )
        await self._close_run(run_id, outcome)
        return outcome

    async def ingest_connector(
        self,
        connector: SourceConnector,
        source_id: str,
        run_id: str,
        *,
        classification: str = "internal",
    ) -> IngestOutcome:
        """Ingest from a remote repository, honouring its per-item access control.

        The counterpart to `ingest_directory`, and the reason the connector port
        exists. Two differences from a directory walk, both consequences of the
        source knowing who may read what:

        * A document whose permissions could not be determined is **failed, not
          skipped quietly**. It appears in the run's failure list so an
          administrator sees "3 documents could not be assessed" rather than
          three documents silently missing — or, far worse, three documents
          present with the wrong audience.
        * Content is fetched per document rather than discovered up front, so an
          unchanged document costs a comparison instead of a transfer.
        * A document that has **disappeared** from the source is expired. A
          directory walk has the same problem and does not solve it either, but
          a remote library is where it actually bites: a policy withdrawn in
          the container would otherwise stay in the corpus and keep being cited as
          current, which for a policy assistant is the worst kind of stale.
        """
        outcome = IngestOutcome()
        seen: set[str] = set()
        # Whether the enumeration ran to completion. Everything about expiry
        # depends on this being true — see `_expire_absent`.
        complete = False
        try:
            async for remote in connector.documents():
                outcome.discovered += 1
                # The *family* key, matching what `_expire_absent` compares
                # against. Keyed on the item instead, two blobs sharing a family
                # would leave that family looking absent whenever only one of
                # them was enumerated, and the pipeline would expire a document
                # that is still in the container.
                seen.add(remote.family_key or remote.key)
                if remote.access_error:
                    # Reported before any content is fetched: there is no point
                    # downloading a document nobody will be allowed to read, and the
                    # administrator needs the item named either way.
                    await self._record_failure(
                        run_id,
                        remote.filename,
                        outcome,
                        code="access_unavailable",
                        message=remote.access_error,
                        remedy=(
                            "Grant the AskAU application permission to read this item's "
                            "sharing settings, or remove the anonymous sharing link, then "
                            "reprocess it."
                        ),
                    )
                    continue
                try:
                    data = await remote.fetch() if remote.fetch else b""
                except Exception as exc:
                    await self._record_failure(
                        run_id,
                        remote.filename,
                        outcome,
                        code="fetch_error",
                        message=f"{type(exc).__name__}: {exc}",
                        remedy="Check the item is still present and readable, then reprocess it.",
                    )
                    continue

                item = IngestItem(
                    key=remote.key,
                    family_key=remote.family_key,
                    title=remote.title.rsplit(".", 1)[0] or remote.title,
                    uri=remote.uri,
                    filename=remote.filename,
                    data=data,
                    principals=remote.principals,
                    classification=remote.classification,
                    doc_type=remote.doc_type,
                    department=remote.department,
                    version_label=remote.version_label,
                    effective_from=remote.effective_from,
                    effective_to=remote.effective_to,
                )
                await self._ingest_one(item, source_id, run_id, classification, outcome)

            complete = True
        except Exception as exc:
            # The run row is closed before the error leaves this method, and
            # that ordering is not tidiness.
            #
            # `_close_run` used to be unreachable when the enumeration raised,
            # so the row stayed `running` for ever — and `_start_run` refuses a
            # second run while one is in progress. One throttle we exhausted
            # retries on, one dropped connection, one expired secret, and the
            # source could never be synced again without somebody editing the
            # database by hand. The most routine failure there is, and it
            # bricked the source.
            await self._fail_run(run_id, outcome, exc)
            raise

        complete = True
        outcome.expired = await self._expire_absent(source_id, seen, complete=complete)
        await self._close_run(run_id, outcome)
        return outcome

    async def _expire_absent(self, source_id: str, seen: set[str], *, complete: bool) -> int:
        """Mark documents the source no longer has as `expired`.

        **Only after a complete enumeration**, and that condition is the whole
        of the safety here. "Absent from this run" and "deleted at the source"
        are the same observation, and they are only the same *fact* when the run
        actually finished. A sync that died halfway — a throttle we exhausted
        retries on, a dropped connection, an expired secret — would otherwise
        expire every document it had not reached yet, which is most of the
        corpus. The most destructive possible outcome would be triggered by the
        most ordinary possible failure.

        Expired rather than deleted. The document was real, it was cited, and an
        audit row referring to it must still resolve; FR-018 also makes
        historical content retrievable on request. What changes is that it stops
        being returned by default, which is what `chunks.is_current` controls.

        An empty enumeration expires nothing even when complete. A library that
        legitimately returns zero items is indistinguishable here from a
        misconfigured one that lists nothing, and the two demand opposite
        actions — so this declines to guess and leaves the corpus alone.
        """
        if not complete or not seen:
            return 0

        async with self._engine.begin() as conn:
            rows = (
                await conn.execute(
                    text("""
                    UPDATE documents d SET lifecycle = 'expired'
                    FROM document_families f
                    WHERE f.id = d.family_id
                      AND f.source_id = CAST(:sid AS uuid)
                      AND NOT (f.external_key = ANY(CAST(:seen AS text[])))
                      AND d.lifecycle NOT IN ('expired', 'superseded')
                    RETURNING d.id::text
                    """),
                    {"sid": source_id, "seen": sorted(seen)},
                )
            ).all()

            if rows:
                # Out of default retrieval in the same transaction. A document
                # marked expired whose chunks still say `is_current` is expired
                # in the record and live in the index, and the index is what
                # answers questions.
                await conn.execute(
                    text("""
                    UPDATE chunks SET is_current = FALSE, lifecycle = 'expired'
                    WHERE document_id = ANY(CAST(:ids AS uuid[]))
                    """),
                    {"ids": [r[0] for r in rows]},
                )
        return len(rows)

    async def _ingest_one(
        self,
        item: IngestItem,
        source_id: str,
        run_id: str,
        classification: str,
        outcome: IngestOutcome,
    ) -> None:
        """Task bookkeeping and failure capture, shared by both entry points.

        Extracted so a connector run and a directory run cannot drift in how
        they record failures — a discrepancy there would make two ingestion
        histories that cannot be compared.
        """
        # A classification the schema does not know is refused, not defaulted.
        #
        # It arrives as a string from a source we do not control — blob
        # metadata somebody typed — and `CAST(:cls AS classification)` would
        # abort the whole run on a typo. Falling back to the run default is the
        # tempting repair and the wrong one: "highly-restricted" with a hyphen
        # would silently become `internal` and the document would be retrievable
        # by everyone the run's default admits.
        effective = item.classification or classification
        if effective not in _CLASSIFICATIONS:
            await self._record_failure(
                run_id,
                item.filename,
                outcome,
                code="invalid_classification",
                message=f"'{item.classification}' is not a classification",
                remedy=(
                    "Set the document's classification metadata to one of "
                    f"{', '.join(sorted(_CLASSIFICATIONS))}, then reprocess it."
                ),
            )
            return

        task_id = await self._open_task(run_id, item.filename)
        try:
            # The document's own classification wins over the run's. The run
            # default is what applies when the source did not say — not a
            # ceiling and not an override.
            await self._one(item, source_id, task_id, effective, outcome)
        except AccessUnavailableError as exc:
            # Its own branch, and its own code. "We could not establish who may
            # read this" is not an extraction error, and reporting it as one
            # would bury the single failure an administrator must act on among
            # the corrupt PDFs.
            outcome.failed += 1
            failure = ValidationFailure(
                code="access_unavailable",
                message=str(exc),
                remedy=(
                    "Grant the AskAU application permission to read this item's sharing "
                    "settings, or remove the anonymous sharing link, then reprocess it."
                ),
            )
            outcome.failures.append((item.filename, failure))
            await self._fail_task(task_id, failure)
            _log.warning("access could not be determined for %s: %s", item.filename, exc)
        except Exception as exc:
            outcome.failed += 1
            failure = ValidationFailure(
                code="extraction_error",
                message=f"{type(exc).__name__}: {exc}",
                remedy="Check the file is not corrupt, then reprocess it.",
            )
            outcome.failures.append((item.filename, failure))
            await self._fail_task(task_id, failure)
            _log.exception("ingestion failed for %s", item.filename)

    async def _record_failure(
        self,
        run_id: str,
        name: str,
        outcome: IngestOutcome,
        *,
        code: str,
        message: str,
        remedy: str,
    ) -> None:
        task_id = await self._open_task(run_id, name)
        outcome.failed += 1
        failure = ValidationFailure(code=code, message=message, remedy=remedy)
        outcome.failures.append((name, failure))
        await self._fail_task(task_id, failure)

    # ── stages ──────────────────────────────────────────────────────────────

    async def _one(
        self,
        item: IngestItem,
        source_id: str,
        task_id: int,
        classification: str,
        outcome: IngestOutcome,
    ) -> None:
        data = item.data

        checked = validation.validate_file(item.filename, data)
        if isinstance(checked, ValidationFailure):
            outcome.failed += 1
            outcome.failures.append((item.filename, checked))
            await self._fail_task(task_id, checked)
            return

        # Same key the family is stored under, or an unchanged document looks
        # new every run: a fresh revision each time, the previous one superseded,
        # and the version history filling with identical copies.
        existing = await self._unchanged(
            source_id, item.family_key or item.key, checked.content_hash
        )
        if existing is not None:
            # Unchanged content, which is not the same as an unchanged document.
            #
            # A permission change is the most common thing a repository does —
            # somebody leaves a team, a library is re-shared, an over-broad
            # grant is tightened — and none of it alters a byte of the file. The
            # content hash is blind to all of it.
            #
            # Skipping the access list here made the failure permanent and
            # silent: the revoked grantee kept access until somebody happened to
            # edit the document, `document_acl` stayed stale, and the reconciler
            # faithfully copied the stale value into `chunks.acl_principals` —
            # so the denormalised copy agreed with the authoritative table and
            # nothing anywhere looked wrong.
            #
            # Only for sources that carry per-item access. A filesystem export
            # (`principals is None`) inherits the source's rules, which have not
            # moved, so rewriting them every sync would be work for nothing.
            if item.principals is not None:
                async with self._engine.begin() as conn:
                    await self._write_acl(conn, existing, source_id, item.principals)
                # `AclReconciler`, not a private copy. It recomputes the array
                # from `document_acl` exactly as this did, and it does two more
                # things the copy had silently dropped: it stamps
                # `acl_synced_at`, without which these chunks look permanently
                # stale to the lag metric that alerts on outstanding
                # revocations, and it bumps `users.acl_version` to invalidate
                # cached authorization contexts.
                await AclReconciler(self._engine).reconcile_document(existing)
            outcome.skipped += 1
            await self._set_stage(task_id, "skipped_unchanged")
            return

        await self._set_stage(task_id, "extracting")
        try:
            extraction = extract(data, item.filename)
        except UnsupportedFormatError as exc:
            failure = ValidationFailure(
                code="unsupported_format",
                message=str(exc),
                remedy="Convert the document, or exclude it with the source owner.",
            )
            outcome.failed += 1
            outcome.failures.append((item.filename, failure))
            await self._fail_task(task_id, failure)
            return

        problem = validation.validate_extraction(item.filename, extraction)

        # A scan reaches here with no text. OCR is the second attempt — and only
        # for `no_text_layer`, never for `insufficient_text`: a cover sheet that
        # genuinely says little would be re-read at great cost to confirm it
        # still says little, and OCR of a page that has real text replaces
        # something accurate with a guess.
        if problem is not None and problem.code == "no_text_layer" and self._ocr is not None:
            await self._set_stage(task_id, "ocr")
            try:
                extraction = await ocr_engine.extract_pdf(self._ocr, data, item.filename)
                problem = validation.validate_extraction(item.filename, extraction)
            except ocr_engine.OcrUnavailableError as exc:
                # Distinguished from "this document is a scan": one is an
                # infrastructure fault the platform team fixes, the other is a
                # document the source owner must replace. Reporting the former
                # as the latter sends people to argue with the wrong party.
                _log.warning("%s: OCR failed: %s", item.filename, exc)
                problem = ValidationFailure(
                    code="ocr_unavailable",
                    message=f"{item.filename} needs OCR but the OCR service failed",
                    remedy=(
                        "This is an infrastructure fault, not a document problem. "
                        "Check /health/deep — the OCR service is unreachable or erroring."
                    ),
                )

        if problem is not None:
            outcome.failed += 1
            outcome.failures.append((item.filename, problem))
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
            outcome.failures.append((item.filename, failure))
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
            item,
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

    async def _unchanged(self, source_id: str, key: str, digest: bytes) -> str | None:
        """The existing document id when the content is byte-identical, else None.

        Returns the id rather than a boolean because an unchanged document is
        not necessarily an unchanged *document row* — its access list may have
        moved even though not one byte did, and the caller needs something to
        write that against.
        """
        async with self._engine.connect() as conn:
            return (
                await conn.execute(
                    text("""
                        SELECT d.id::text FROM documents d
                        JOIN document_families f ON f.id = d.family_id
                        WHERE f.source_id = CAST(:sid AS uuid)
                          AND f.external_key = :key
                          AND d.content_hash = :hash
                          AND d.ingest_status = 'indexed'
                        """),
                    {"sid": source_id, "key": key, "hash": digest},
                )
            ).scalar_one_or_none()

    async def _write(
        self,
        item: IngestItem,
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
                    # `family_key` when the source names one, otherwise the
                    # item key — which is the same value for every source that
                    # versions by overwriting.
                    {
                        "sid": source_id,
                        "key": item.family_key or item.key,
                        "title": item.title,
                    },
                )
            ).scalar_one()

            # A new revision of an existing document, not a replacement.
            #
            # `documents.version_seq` defaults to 1 and nothing set it, so a
            # family could hold exactly one version and **re-ingesting a changed
            # document failed on the unique constraint, permanently**. Re-sync
            # is the whole of FR-049, so that broke the feature the connector
            # exists to serve — and no test caught it because none had ever
            # ingested the same document twice with different content.
            #
            # It also explains a gap elsewhere: `lifecycle = 'superseded'` could
            # never arise naturally, which left the `outdated` answer state
            # without a producer.
            version_seq = int(
                (
                    await conn.execute(
                        text(
                            "SELECT coalesce(max(version_seq), 0) + 1 "
                            "FROM documents WHERE family_id = :fam"
                        ),
                        {"fam": family_id},
                    )
                ).scalar_one()
            )

            if version_seq > 1:
                # The previous revision stays in the corpus rather than being
                # deleted: FR-018 makes historical content opt-in retrievable,
                # and a superseded policy is exactly what somebody asking "what
                # was the rule last year" needs.
                await conn.execute(
                    text("""
                    UPDATE documents SET lifecycle = 'superseded'
                    WHERE family_id = :fam AND lifecycle <> 'superseded'
                    """),
                    {"fam": family_id},
                )
                # Its chunks stop being current in the same transaction. A
                # superseded document whose chunks still say `is_current` would
                # be returned by default retrieval, which is the failure mode
                # that puts an out-of-date policy in front of a reader as
                # though it were the rule.
                #
                # And their `lifecycle` follows, which is a second thing and not
                # a tidier version of the first. Retrieval reads the *chunk's*
                # lifecycle, so a citation's status — the "Superseded" the
                # interface shows — comes from here and never from `documents`.
                # Leaving it `active` is not a disclosure, because `is_current`
                # still holds the content out of default retrieval; it means
                # material fetched with `include_historical` comes back labelled
                # *Active*, and `outdated_from_citations` promotes an answer to
                # the `outdated` state by looking for exactly that label. The
                # banner would never fire.
                await conn.execute(
                    text("""
                    UPDATE chunks SET is_current = FALSE, lifecycle = 'superseded'
                    WHERE family_id = :fam
                    """),
                    {"fam": family_id},
                )

            document_id = str(
                (
                    await conn.execute(
                        text("""
                        INSERT INTO documents
                            (family_id, source_id, version_seq, title, language,
                             source_uri, mime_type,
                             byte_size, page_count, content_hash, classification,
                             lifecycle, ingest_status, injection_risk, review_required,
                             indexed_at, last_synced_at, chunk_count,
                             doc_type, department, version_label,
                             effective_from, effective_to)
                        VALUES (:fam, CAST(:sid AS uuid), :vseq, :title, :lang, :uri, :mime,
                                :size, :pages, :hash, CAST(:cls AS classification),
                                CAST(:lifecycle AS lifecycle_status),
                                'indexed', :risk, :review, now(), now(), :n,
                                :dtype, :dept, :vlabel,
                                CAST(:efrom AS date), CAST(:eto AS date))
                        RETURNING id
                        """),
                        {
                            "fam": family_id,
                            "sid": source_id,
                            "vseq": version_seq,
                            "title": item.title,
                            "lang": language,
                            "uri": item.uri,
                            "dtype": item.doc_type,
                            "dept": item.department,
                            "vlabel": item.version_label,
                            # asyncpg binds a `date` parameter as a date and
                            # refuses a string, cast or no cast.
                            "efrom": _as_date(item.effective_from),
                            "eto": _as_date(item.effective_to),
                            "mime": _mime_for(checked.suffix),
                            "size": checked.byte_size,
                            "pages": extraction.page_count,
                            "hash": checked.content_hash,
                            "cls": classification,
                            # See PHASE1_ASSUMED_LIFECYCLE — a deliberate
                            # assumption, not a default nobody thought about.
                            "lifecycle": PHASE1_ASSUMED_LIFECYCLE,
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
            await self._write_acl(conn, document_id, source_id, item.principals)

            for chunk, vector in zip(chunk_list, vector_list, strict=True):
                await conn.execute(
                    text("""
                    INSERT INTO chunks
                        (document_id, family_id, ordinal, content, token_count,
                         heading_path, section_ref, page_from, page_to,
                         char_start, char_end, classification, lifecycle, language,
                         version_seq, is_current, acl_principals, embedding,
                         embedding_model, lang_config, tsv)
                    VALUES (CAST(:doc AS uuid), :fam, :ord, :content, :tokens,
                            CAST(:heads AS text[]), :section, :pfrom, :pto,
                            :cstart, :cend, CAST(:cls AS classification), 'active', :lang,
                            :vseq, TRUE,
                            (SELECT coalesce(array_agg(principal_id), '{}')
                             FROM document_acl WHERE document_id = CAST(:doc AS uuid)),
                            CAST(:emb AS vector), :embmodel, CAST(:tscfg AS regconfig),
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
                        "vseq": version_seq,
                        "emb": "[" + ",".join(f"{v:.6f}" for v in vector) + "]",
                        # Stored with the vector, not inferred from configuration
                        # later: configuration changes, and a vector whose model
                        # is unknown cannot be compared, refreshed or migrated.
                        "embmodel": self._embedder.model_name,
                        "tscfg": ts_config,
                    },
                )
        return document_id

    async def _write_acl(
        self,
        conn: AsyncConnection,
        document_id: str,
        source_id: str,
        principals: tuple[SourcePrincipal, ...] | None,
    ) -> None:
        """Give the document its audience.

        The branch here is the security boundary of the whole connector, and it
        turns on `None` versus a tuple rather than on emptiness:

        * `None` — the source has no per-item access control (a filesystem
          export). Inherit `knowledge_sources.access_rules`, which is what
          always happened.
        * a tuple — the connector determined the audience for *this document*.
          Use exactly that, **including when it is empty**. An empty audience
          means nobody may read it, which is the correct and safe reading of "we
          found no grantee we could map".

        Writing `principals or source_rules` instead would collapse those two
        cases and publish every unmappable document to the whole library. It is
        a single word, it reads as a sensible default, and it is the disclosure
        this design exists to prevent.

        Rows are replaced rather than added to. A grant removed at the source
        must disappear here on the next sync, and an ACL that only ever grows is
        an ACL that never revokes.
        """
        if principals is None:
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
            return

        # Applied to every revision in the family, not only the new one.
        #
        # A family is one item in the source repository, and a repository's
        # permissions are on the item — revisions are not separately
        # permissioned. So the item's *current* permissions govern its history
        # too, and scoping this to the new document would leave a hole with a
        # simple shape: somebody whose access was revoked could still retrieve
        # last year's version of the same policy through `include_historical`,
        # which defeats the revocation entirely.
        await conn.execute(
            text("""
            DELETE FROM document_acl
            WHERE document_id IN (
                SELECT id FROM documents WHERE family_id = (
                    SELECT family_id FROM documents WHERE id = CAST(:d AS uuid)
                )
            )
            """),
            {"d": document_id},
        )
        for principal in principals:
            # Upserted, not looked up. A grantee the directory has and we have
            # never seen is a normal state — somebody joined, a group was
            # created — and dropping the grant because the row is missing would
            # make access depend on the order two systems were synced in.
            principal_id = (
                await conn.execute(
                    text("""
                    INSERT INTO principals (kind, external_id, display_name)
                    VALUES (CAST(:kind AS principal_kind), :ext, :name)
                    ON CONFLICT (kind, external_id) DO UPDATE
                      SET display_name = EXCLUDED.display_name, synced_at = now()
                    RETURNING id
                    """),
                    {
                        "kind": principal.kind,
                        "ext": principal.external_id,
                        "name": principal.display_name or principal.external_id,
                    },
                )
            ).scalar_one()
            await conn.execute(
                text("""
                INSERT INTO document_acl (document_id, principal_id, source_of_truth)
                SELECT d.id, :p, 'source_repository'
                FROM documents d
                WHERE d.family_id = (
                    SELECT family_id FROM documents WHERE id = CAST(:d AS uuid)
                )
                ON CONFLICT DO NOTHING
                """),
                {"d": document_id, "p": principal_id},
            )

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

    async def _fail_run(self, run_id: str, outcome: IngestOutcome, exc: Exception) -> None:
        """Close a run that died, keeping the partial counts.

        `failed` has been in the status constraint since the first migration and
        nothing had ever written it. The counts are kept rather than zeroed: a
        run that indexed nine hundred documents and then died did index nine
        hundred documents, and an operator deciding whether to retry or
        investigate needs to know how far it got.
        """
        async with self._engine.begin() as conn:
            await conn.execute(
                text("""
                UPDATE ingestion_runs
                SET status = 'failed', finished_at = now(),
                    docs_discovered = :disc, docs_processed = :ok,
                    docs_skipped = :skip, docs_failed = :bad,
                    error = CAST(:error AS jsonb)
                WHERE id = CAST(:r AS uuid)
                """),
                {
                    "r": run_id,
                    "disc": outcome.discovered,
                    "ok": outcome.indexed,
                    "skip": outcome.skipped,
                    "bad": outcome.failed,
                    # The type and message, never a traceback: this is read back
                    # through the admin API and a stack trace on the wire is an
                    # information disclosure.
                    "error": json.dumps({"type": type(exc).__name__, "message": str(exc)[:500]}),
                },
            )
        _log.exception("ingestion run %s failed during enumeration", run_id)

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
    """Supported files under `root`, sorted.

    The existence check is here rather than at the call site for two reasons:
    it is a blocking stat and belongs on the same worker thread as the walk, and
    `Path.rglob` on a directory that does not exist yields nothing rather than
    raising. Without it an unmounted share reports a *successful* sync that
    found zero documents — the corpus stays safe, because an empty enumeration
    expires nothing, but the run record says the export is empty when it is
    actually absent, and those two call for opposite responses.
    """
    if not root.is_dir():
        raise NotADirectoryError(f"{root} is not a readable directory")
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
