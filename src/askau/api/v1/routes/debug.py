"""Retrieval inspection — admin only.

Exists so the Month 2 gate is demonstrable over HTTP: run the same question as
two identities and compare what comes back. That comparison is the whole design
in one observation, and it is worth being able to make without a debugger.

Admin-gated because the response exposes retrieval internals. It exposes nothing
the caller is not already authorized to read — the same ACL predicate applies —
but scores and rankings are operational detail, not end-user content.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from askau.api.deps import AuthzDep, SettingsDep
from askau.api.schemas.wire import DebugRetrieveOut, RetrievedChunkOut
from askau.core.rbac import require_system_admin
from askau.domain.enums import ts_config_for
from askau.domain.retrieval import RetrievalQuery

router = APIRouter(prefix="/v1/debug", tags=["debug"])


@router.get("/retrieve", response_model=DebugRetrieveOut)
async def retrieve(
    authz: AuthzDep,
    settings: SettingsDep,
    request: Request,
    q: str = Query(min_length=1, max_length=1000),
    language: str = "en",
    include_historical: bool = False,
) -> DebugRetrieveOut:
    require_system_admin(authz)

    embedder = request.app.state.orchestrator._embedder
    result = await request.app.state.retriever.search(
        RetrievalQuery(
            text=q,
            embedding=await embedder.embed_query(q),
            authz=authz,
            candidate_k=settings.retrieval_candidate_k,
            top_k=settings.retrieval_top_k,
            include_historical=include_historical,
            ts_config=ts_config_for(language),
        )
    )
    return DebugRetrieveOut(
        question=q,
        strategy=result.strategy.value,
        took_ms=result.took_ms,
        count=len(result),
        chunks=[
            RetrievedChunkOut(
                chunk_id=int(c.chunk_id),
                document_id=str(c.document_id),
                document_title=c.document_title,
                classification=c.classification.value,
                section_ref=c.section_ref,
                page_from=c.page_from,
                score=round(c.score, 6),
                matched_both_arms=c.matched_both_arms,
                excerpt=c.content[:180],
            )
            for c in result.chunks
        ],
    )
