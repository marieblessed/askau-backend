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
from fastapi.exceptions import RequestValidationError
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
from askau.api.v1.routes import knowledge_bases as knowledge_bases_routes
from askau.audit.writer import AuditWriter
from askau.core import cache
from askau.core.authz import AuthorizationResolver
from askau.core.correlation import HEADER, get_correlation_id, set_correlation_id
from askau.core.errors import AskAUError, InvalidRequestError
from askau.core.identity import build_verifier
from askau.db.conversations import ConversationRepository
from askau.db.engine import assert_database_ready, dispose_engines, get_engine
from askau.db.eval_sampling import EvalSampler
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
    app.state.eval_sampler = EvalSampler(engine)

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
        headers: dict[str, str] = {}
        # `Retry-After` as a header, not only in the body. Their
        # `RateLimitedError` carries a `retryAfter` field and the header is the
        # standard place to read it from — a client that backs off correctly is
        # worth more to us than one that has to parse a body to find out how long.
        retry_after = getattr(exc, "retry_after", None)
        if retry_after is not None:
            headers["Retry-After"] = str(int(retry_after))

        return JSONResponse(
            status_code=exc.status,
            content=exc.to_problem(get_correlation_id(), str(request.url.path)),
            media_type="application/problem+json",
            headers=headers or None,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Field validation, in our error contract rather than FastAPI's.

        Without this, FastAPI answers its own validation failures with
        `{"detail": [{...}]}` — a shape their `lib/api/errors.ts` cannot read.
        It looks for `message` and `fieldErrors`; finding neither, it produces
        `UnknownApiError`, and their production build suppresses the message. So
        the single most common error a client hits — a field that is empty, too
        long, or missing — reached the user as "An unexpected error occurred",
        with the explanation we had already written discarded on the way.

        `InvalidRequestError` was moved to 422 for exactly this reason, which is
        what made the omission easy to miss: errors we *raise* were already
        right, and errors FastAPI raises for us were never converted.
        """
        field_errors: dict[str, list[str]] = {}
        for err in exc.errors():
            # Drop the leading "body" / "query" segment: their form binds by
            # field name, and `body.content` matches no input on their side.
            location = [str(p) for p in err["loc"][1:]] or [str(p) for p in err["loc"]]
            field_errors.setdefault(".".join(location), []).append(err["msg"])

        problem = InvalidRequestError(
            "The request could not be processed. Check the highlighted fields."
        ).to_problem(get_correlation_id(), str(request.url.path))
        # `fieldErrors` last: it is the member their form rendering depends on,
        # and `to_problem` has no business knowing about it.
        problem["fieldErrors"] = field_errors
        return JSONResponse(status_code=422, content=problem, media_type="application/problem+json")

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
                # Same extension members as every other error, so a client never
                # has to special-case the one response it is most likely to hit
                # when something is badly wrong.
                "code": "SERVER_ERROR",
                "message": "An unexpected error occurred.",
                "correlationId": correlation,
            },
            media_type="application/problem+json",
        )

    # Probes and metrics stay at the root, unprefixed. They are scraped by the
    # kubelet and by Prometheus, neither of which knows or should know about an
    # API version — and `/api/*` is the path the web client's proxy forwards, so
    # putting a liveness probe behind it would route infrastructure traffic
    # through an application concern.
    app.include_router(health.router)

    # Everything else is the published API surface, under `/api`.
    #
    # `/api` and not bare `/v1`, because the client sends every request to
    # `NEXT_PUBLIC_API_BASE_URL` and expects to reach us at `/api/v1`. The
    # version stays in each router's own prefix so a future `/api/v2` is an
    # addition rather than a rename.
    #
    # Note our auth routes live at `/api/v1/auth/*`, deliberately *not*
    # `/api/auth/*`: NextAuth owns that path on the client and its middleware
    # short-circuits it, so anything of ours there would be unreachable.
    for router in (
        health.api_router,
        auth.router,
        auth.users_router,
        session.router,
        ask.router,
        documents.router,
        conversations.router,
        conversations.feedback_router,
        knowledge.router,
        knowledge_bases_routes.router,
        admin.router,
        security.router,
        evaluation.router,
        debug.router,
    ):
        app.include_router(router, prefix="/api")
    return app


app = create_app()
