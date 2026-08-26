"""Application factory.

Startup order matters: the database is verified *before* anything is wired, so
a misprovisioned instance fails with a message naming the problem rather than
serving degraded answers. Infrastructure lives outside this repository
(ADR-0014), which makes that check the application's responsibility.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from askau.api.v1.routes import (
    admin,
    ask,
    auth,
    conversations,
    debug,
    documents,
    evaluation,
    health,
    knowledge,
    security,
    session,
)
from askau.audit.writer import AuditWriter
from askau.core import cache
from askau.core.authz import AuthorizationResolver
from askau.core.correlation import HEADER, get_correlation_id, set_correlation_id
from askau.core.errors import AskAUError
from askau.core.identity import build_verifier
from askau.db.conversations import ConversationRepository
from askau.db.engine import assert_database_ready, dispose_engines, get_engine
from askau.llm.registry import build_embedder, build_llm
from askau.llm.usage import UsageLedger
from askau.rag.orchestrator import RagOrchestrator
from askau.retrieval.adapters.pgvector_hybrid import PgVectorHybridRetriever
from askau.retrieval.adapters.rerank_noop import NoopReranker
from askau.settings import get_settings

_log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    # Verify before wiring: pgvector below 0.8 degrades retrieval silently, so
    # the only safe time to discover it is now.
    await assert_database_ready(settings)

    engine = get_engine(settings)
    redis = cache.get_redis(settings)
    audit = AuditWriter(engine)
    await audit.start()
    usage = UsageLedger(engine)
    await usage.start()

    app.state.settings = settings
    app.state.engine = engine
    app.state.audit = audit
    app.state.verifier = build_verifier(settings)
    app.state.redis = redis
    app.state.authz_resolver = AuthorizationResolver(engine, redis, settings)
    app.state.retriever = PgVectorHybridRetriever(engine, settings)
    app.state.orchestrator = RagOrchestrator(
        retriever=app.state.retriever,
        embedder=build_embedder(settings),
        llm=build_llm(settings),
        settings=settings,
        reranker=NoopReranker(),
        usage=usage,
    )
    app.state.usage = usage
    app.state.conversations = ConversationRepository(engine)

    _log.info(
        "AskAU ready — env=%s auth=%s llm=%s embed=%s",
        settings.env,
        settings.auth_mode,
        settings.llm_provider,
        settings.embedding_provider,
    )
    try:
        yield
    finally:
        await usage.stop()
        await audit.stop()
        await cache.close_redis()
        await dispose_engines()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="AskAU Core API",
        version="0.1.0",
        description=(
            "Enterprise permission-aware RAG platform for the African Union "
            "Commission. Authorization is enforced in the retrieval query; the "
            "language model never makes an access decision."
        ),
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000"] if settings.env == "development" else [],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def correlation_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        correlation = set_correlation_id(request.headers.get(HEADER))
        response = await call_next(request)
        response.headers[HEADER] = correlation
        return response

    @app.exception_handler(AskAUError)
    async def askau_error_handler(request: Request, exc: AskAUError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content=exc.to_problem(get_correlation_id(), str(request.url.path)),
            media_type="application/problem+json",
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        # Never leak a stack trace or an internal message: the correlation ID is
        # what ties the response to the full detail in the logs.
        correlation = get_correlation_id()
        _log.exception("unhandled error [%s]", correlation)
        return JSONResponse(
            status_code=500,
            content={
                "type": "https://askau.au.int/errors/internal_error",
                "title": "Internal error",
                "status": 500,
                "detail": "An unexpected error occurred.",
                "correlation_id": correlation,
            },
            media_type="application/problem+json",
        )

    for router in (
        health.router,
        auth.router,
        session.router,
        ask.router,
        documents.router,
        conversations.router,
        conversations.feedback_router,
        knowledge.router,
        admin.router,
        security.router,
        evaluation.router,
        debug.router,
    ):
        app.include_router(router)
    return app


app = create_app()
