"""Load the synthetic corpus and identities.

Runs the real ingestion path — markdown to blocks, structure-aware chunking,
embedding, indexing, ACL materialization — rather than inserting pre-baked rows.
A seed that bypasses the pipeline proves nothing about the pipeline.

Idempotent: re-running truncates and rebuilds, so it is safe in a loop.
"""

from __future__ import annotations

import asyncio
import re
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from askau.db.engine import assert_database_ready, dispose_engines, get_admin_engine
from askau.domain.enums import ts_config_for
from askau.domain.knowledge import ExtractedBlock
from askau.ingestion.chunking import StructureAwareChunker
from askau.ingestion.language import detect_language
from askau.llm.ports import Embedder
from askau.llm.registry import build_embedder
from askau.scripts.corpus import DOCUMENTS, PRINCIPALS, USERS, SeedDoc
from askau.settings import get_settings

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_SECTION_NO = re.compile(r"^(\d+(?:\.\d+)*)\.?\s")


def markdown_to_blocks(body: str) -> tuple[ExtractedBlock, ...]:
    """Parse the fixture markdown into positioned blocks.

    Mirrors what a real extractor produces: headings that establish a path, and
    body blocks that inherit it. Page numbers are synthesized (one page per two
    sections) so page-anchored citations have something to carry.
    """
    blocks: list[ExtractedBlock] = []
    path: list[str] = []
    offset = 0
    section_ref: str | None = None
    section_count = 0

    for raw in body.split("\n"):
        line = raw.strip()
        if not line:
            offset += len(raw) + 1
            continue

        heading = _HEADING.match(line)
        if heading:
            level, title = len(heading.group(1)), heading.group(2).strip()
            path = [*path[: level - 1], title]
            match = _SECTION_NO.match(title)
            section_ref = match.group(1) if match else None
            section_count += 1
            blocks.append(
                ExtractedBlock(
                    text=title,
                    page=1 + section_count // 2,
                    heading_path=tuple(path),
                    section_ref=section_ref,
                    is_heading=True,
                    char_start=offset,
                    char_end=offset + len(title),
                )
            )
        else:
            blocks.append(
                ExtractedBlock(
                    text=line,
                    page=1 + section_count // 2,
                    heading_path=tuple(path),
                    section_ref=section_ref,
                    char_start=offset,
                    char_end=offset + len(line),
                )
            )
        offset += len(raw) + 1

    return tuple(blocks)


async def _reset(conn: AsyncConnection) -> None:
    await conn.execute(
        text("""
        TRUNCATE chunks, document_acl, documents, document_families,
                 knowledge_sources, message_feedback, citations, messages,
                 conversations, app_role_assignments, user_principals, sessions,
                 users, principals, ingestion_tasks, ingestion_runs
        RESTART IDENTITY CASCADE
    """)
    )


async def _seed_identities(conn: AsyncConnection) -> dict[str, int]:
    principal_ids: dict[str, int] = {}

    for p in PRINCIPALS:
        pid = (
            await conn.execute(
                text("""INSERT INTO principals (kind, external_id, display_name)
                        VALUES (:kind, :ext, :name) RETURNING id"""),
                {"kind": p.kind, "ext": p.key, "name": p.name},
            )
        ).scalar_one()
        principal_ids[p.key] = int(pid)

    for u in USERS:
        # Every user gets their own user-principal as well as group principals:
        # document-level grants to a named individual are a real requirement
        # (FR-002a), not only group grants.
        own = (
            await conn.execute(
                text("""INSERT INTO principals (kind, external_id, display_name)
                        VALUES ('user', :ext, :name) RETURNING id"""),
                {"ext": f"usr-{u.username}", "name": u.name},
            )
        ).scalar_one()
        principal_ids[f"usr-{u.username}"] = int(own)

        uid = (
            await conn.execute(
                text("""INSERT INTO users
                          (principal_id, entra_oid, email, display_name, department)
                        VALUES (:pid, :oid, :email, :name, :dept) RETURNING id"""),
                {
                    "pid": own,
                    "oid": f"oid-{u.username}",
                    "email": u.email,
                    "name": u.name,
                    "dept": u.department,
                },
            )
        ).scalar_one()

        for group in (f"usr-{u.username}", *u.groups):
            await conn.execute(
                text("""INSERT INTO user_principals (user_id, principal_id)
                        VALUES (:uid, :pid) ON CONFLICT DO NOTHING"""),
                {"uid": uid, "pid": principal_ids[group]},
            )
        for role in u.roles:
            await conn.execute(
                text("""INSERT INTO app_role_assignments (user_id, role)
                        VALUES (:uid, :role) ON CONFLICT DO NOTHING"""),
                {"uid": uid, "role": role},
            )

    return principal_ids


async def _seed_source(conn: AsyncConnection) -> str:
    owner = (
        await conn.execute(
            text("SELECT id FROM users WHERE email = :e"),
            {"e": "admin.knowledge@africanunion.org"},
        )
    ).scalar_one()
    # status='active' requires approved_by — the BR-001 CHECK constraint.
    return str(
        (
            await conn.execute(
                text("""INSERT INTO knowledge_sources
                        (name, source_type, description, business_owner_id, department,
                         default_classification, location, status, approved_by, approved_at)
                        VALUES ('AUC Policy Library (synthetic)', 'filesystem',
                                'Synthetic corpus for development and testing',
                                :owner, 'MISD', 'internal', '{"path": "synthetic"}',
                                'active', :owner, now())
                        RETURNING id"""),
                {"owner": owner},
            )
        ).scalar_one()
    )


async def _index_document(
    conn: AsyncConnection,
    doc: SeedDoc,
    source_id: str,
    principal_ids: dict[str, int],
    chunker: StructureAwareChunker,
    embedder: Embedder,
    run_id: str,
) -> int:
    family_id = (
        await conn.execute(
            text("""INSERT INTO document_families (source_id, external_key, canonical_title)
                    VALUES (:src, :key, :title)
                    ON CONFLICT (source_id, external_key) DO UPDATE
                      SET canonical_title = EXCLUDED.canonical_title
                    RETURNING id"""),
            {"src": source_id, "key": doc.family_key, "title": doc.title},
        )
    ).scalar_one()

    guess = detect_language(doc.body, declared=doc.language)
    doc_id = (
        await conn.execute(
            text("""INSERT INTO documents
                    (family_id, source_id, title, doc_type, language, source_uri,
                     mime_type, content_hash, classification, department, version_label,
                     version_seq, lifecycle, effective_from, effective_to,
                     ingest_status, injection_risk, review_required, indexed_at)
                    VALUES (:fam, :src, :title, :dtype, :lang, :uri, 'text/markdown',
                            digest(:body, 'sha256'), :cls, :dept, :vlabel, :vseq,
                            CAST(:life AS lifecycle_status), :efrom, :eto,
                            'indexed', :risk, :review, now())
                    RETURNING id"""),
            {
                "fam": family_id,
                "src": source_id,
                "title": doc.title,
                "dtype": doc.doc_type,
                "lang": doc.language,
                "uri": f"https://sharepoint.africanunion.org/policies/{doc.key}",
                "body": doc.body,
                "cls": doc.classification,
                "dept": doc.department,
                "vlabel": doc.version_label,
                "vseq": doc.version_seq,
                "life": doc.lifecycle,
                "efrom": doc.effective_from,
                "eto": doc.effective_to,
                "risk": doc.injection_risk,
                "review": doc.injection_risk >= 70,
            },
        )
    ).scalar_one()

    if doc.lifecycle == "active":
        await conn.execute(
            text("UPDATE document_families SET current_document_id = :d WHERE id = :f"),
            {"d": doc_id, "f": family_id},
        )

    # document_acl is authoritative; chunks.acl_principals is derived from it.
    acl = [principal_ids[g] for g in doc.acl_groups]
    for pid in acl:
        await conn.execute(
            text("""INSERT INTO document_acl (document_id, principal_id)
                    VALUES (:d, :p) ON CONFLICT DO NOTHING"""),
            {"d": doc_id, "p": pid},
        )

    task_id = (
        await conn.execute(
            text("""INSERT INTO ingestion_tasks (run_id, document_id, external_key, stage)
                    VALUES (CAST(:run AS uuid), :doc, :key, 'chunking') RETURNING id"""),
            {"run": run_id, "doc": doc_id, "key": doc.key},
        )
    ).scalar_one()

    chunks = chunker.chunk(markdown_to_blocks(doc.body))
    if not chunks:
        await conn.execute(
            text("""UPDATE ingestion_tasks
                    SET stage = 'failed', error_code = 'no_extractable_text',
                        error_detail = :detail, updated_at = now()
                    WHERE id = :id"""),
            {
                "id": task_id,
                "detail": '{"remedy": "Check the document has a text layer; '
                'enable OCR for this source if it is a scan."}',
            },
        )
        return 0

    vectors = await embedder.embed_documents([c.content for c in chunks])
    ts_config = ts_config_for(doc.language) if guess.confident else "simple"
    is_current = doc.lifecycle == "active"

    for chunk, vector in zip(chunks, vectors, strict=True):
        await conn.execute(
            text("""INSERT INTO chunks
                    (document_id, family_id, ordinal, content, token_count,
                     heading_path, section_ref, page_from, page_to, char_start, char_end,
                     classification, lifecycle, department, language,
                     effective_from, effective_to, version_seq, is_current,
                     acl_principals, embedding, lang_config, tsv)
                    VALUES
                    (:doc, :fam, :ord, :content, :tokens,
                     CAST(:heads AS text[]), :section, :pfrom, :pto, :cstart, :cend,
                     :cls, CAST(:life AS lifecycle_status), :dept, :lang,
                     :efrom, :eto, :vseq, :current,
                     CAST(:acl AS bigint[]), CAST(:emb AS vector),
                     CAST(:tscfg AS regconfig),
                     to_tsvector(CAST(:tscfg AS regconfig), :content))"""),
            {
                "doc": doc_id,
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
                "cls": doc.classification,
                "life": doc.lifecycle,
                "dept": doc.department,
                "lang": doc.language,
                "efrom": doc.effective_from,
                "eto": doc.effective_to,
                "vseq": doc.version_seq,
                "current": is_current,
                "acl": acl,
                "emb": "[" + ",".join(f"{v:.7g}" for v in vector) + "]",
                "tscfg": ts_config,
            },
        )

    await conn.execute(
        text("UPDATE documents SET chunk_count = :n WHERE id = :d"),
        {"n": len(chunks), "d": doc_id},
    )
    await conn.execute(
        text("""UPDATE ingestion_tasks SET stage = 'indexed', updated_at = now()
                WHERE id = :id"""),
        {"id": task_id},
    )
    return len(chunks)


async def main() -> int:
    settings = get_settings()
    await assert_database_ready(settings)

    engine = get_admin_engine(settings)
    embedder = build_embedder(settings)
    chunker = StructureAwareChunker(
        settings.chunk_target_tokens,
        settings.chunk_overlap_tokens,
        settings.chunk_max_tokens,
    )

    async with engine.begin() as conn:
        await _reset(conn)
        principal_ids = await _seed_identities(conn)
        source_id = await _seed_source(conn)

        # Record the load as a real ingestion run. The administration console
        # reads this table, and a console whose ingestion history is always empty
        # cannot be reviewed — the seed has to leave the same trail the
        # scheduled pipeline will.
        run_id = (
            await conn.execute(
                text("""INSERT INTO ingestion_runs
                        (source_id, trigger, status, docs_discovered)
                        VALUES (:src, 'manual', 'running', :n) RETURNING id"""),
                {"src": source_id, "n": len(DOCUMENTS)},
            )
        ).scalar_one()

        total_chunks = 0
        failures = 0
        for doc in DOCUMENTS:
            written = await _index_document(
                conn, doc, source_id, principal_ids, chunker, embedder, str(run_id)
            )
            total_chunks += written
            if written == 0:
                failures += 1

        await conn.execute(
            text("""UPDATE ingestion_runs
                    SET status = :status, docs_processed = :ok, docs_failed = :bad,
                        chunks_written = :chunks, embedding_tokens = :tokens,
                        finished_at = now()
                    WHERE id = :id"""),
            {
                "status": "completed" if failures == 0 else "partial",
                "ok": len(DOCUMENTS) - failures,
                "bad": failures,
                "chunks": total_chunks,
                # Rollup of what the embedder consumed, per FR-053.
                "tokens": sum(d.body.__len__() // 4 for d in DOCUMENTS),
                "id": run_id,
            },
        )

    async with engine.connect() as conn:
        by_class = (
            await conn.execute(
                text("""SELECT classification, count(*) FROM chunks
                        GROUP BY 1 ORDER BY 1""")
            )
        ).all()

    print(f"  identities   {len(USERS)} users, {len(principal_ids)} principals")
    print(f"  documents    {len(DOCUMENTS)}")
    print(f"  chunks       {total_chunks}")
    for classification, count in by_class:
        print(f"    {classification:<20} {count}")
    print(f"  embedder     {embedder.model_name} ({embedder.dimensions}d)")

    await dispose_engines()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
