"""Health endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import text

from askau.api.schemas.wire import HealthOut, HealthResponse
from askau.core import cache

router = APIRouter(tags=["health"])

#: Served under `/api/v1/health` for the web client, separately from the probes
#: above. Same subject, different audience: the probes tell an orchestrator
#: whether to restart or route to this process, and their contract is a status
#: code. This one tells a person's browser whether the service is usable, and
#: its contract is a body shape the client already declares.
api_router = APIRouter(prefix="/v1", tags=["health"])


@router.get("/health/live", response_model=HealthOut)
async def live() -> HealthOut:
    """Liveness: the process only.

    Deliberately checks no dependency. Gating liveness on the database means a
    brief database blip restarts every instance, turning a degradation into an
    outage.
    """
    return HealthOut(status="ok")


@router.get("/health/ready", response_model=HealthOut)
async def ready(request: Request) -> HealthOut:
    checks: dict[str, str] = {}
    settings = request.app.state.settings

    try:
        async with request.app.state.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {type(exc).__name__}"

    checks["redis"] = "ok" if await cache.ping(settings) else "unavailable"
    checks["retriever"] = settings.retriever
    checks["llm"] = f"{settings.llm_provider}:{settings.llm_model}"
    checks["embedder"] = f"{settings.embedding_provider}:{settings.embedding_model}"

    status = "ok" if checks["database"] == "ok" else "error"
    if status == "ok" and checks["redis"] != "ok":
        # Redis loss costs latency, not correctness: the resolver falls back to
        # the database. Degraded, not down.
        status = "degraded"
    return HealthOut(status=status, checks=checks)


@router.get("/health/deep")
async def deep(request: Request) -> dict[str, object]:
    """Per-dependency latency and version — the first call during an incident.

    Unauthenticated like the other health routes, but it reports versions and
    timings rather than anything about content, so there is nothing here an
    attacker gains from.
    """
    import time

    from sqlalchemy import text

    checks: dict[str, object] = {}

    started = time.perf_counter()
    try:
        async with request.app.state.engine.connect() as conn:
            version = (await conn.execute(text("SHOW server_version"))).scalar_one()
            vector = (
                await conn.execute(
                    text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                )
            ).scalar_one_or_none()
        checks["database"] = {
            "ok": True,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "postgres": version,
            "pgvector": vector,
        }
    except Exception as exc:
        checks["database"] = {"ok": False, "error": type(exc).__name__}

    started = time.perf_counter()
    try:
        await request.app.state.redis.ping()
        checks["redis"] = {
            "ok": True,
            "latency_ms": int((time.perf_counter() - started) * 1000),
        }
    except Exception as exc:
        checks["redis"] = {"ok": False, "error": type(exc).__name__}

    settings = request.app.state.settings
    checks["llm"] = {"provider": settings.llm_provider, "model": settings.llm_model}
    checks["embedder"] = {
        "provider": settings.embedding_provider,
        "model": settings.embedding_model,
        "dimensions": settings.embedding_dim,
    }

    # OCR is a separate service. Configured-but-unreachable is the state worth
    # catching: scanned documents would fail ingestion rather than index as
    # nothing, but the run would look like a corpus problem instead of a missing
    # container. Probed here so it reads as infrastructure, which is what it is.
    if settings.ocr_enabled:
        from askau.ingestion.extractors.ocr import engine_for, requested_languages

        languages = requested_languages(settings.ocr_languages)
        engine = engine_for(settings.ocr_provider, settings.ocr_url, languages)
        assert engine is not None
        ok_ocr, detail = await engine.health()
        checks["ocr"] = {
            "ok": ok_ocr,
            "enabled": True,
            "provider": settings.ocr_provider,
            "languages": list(languages),
            "detail": detail,
        }
    else:
        # Off is a decision, not a fault. Reported anyway so the reason a
        # scanned document was rejected is discoverable from one place.
        checks["ocr"] = {"ok": True, "enabled": False, "detail": "OCR is turned off"}
    ok = all(c.get("ok", True) for c in checks.values() if isinstance(c, dict))
    return {"status": "ok" if ok else "degraded", "checks": checks}


@router.get("/metrics", response_class=PlainTextResponse)
async def prometheus(request: Request) -> str:
    """Prometheus exposition. Cluster-internal; counts and latencies only."""
    from askau.observability.metrics import render

    return await render(request.app.state.engine)


#: Bumped with the package. Reported so a support conversation can start with
#: "which build were you on" rather than establishing it.
_VERSION = "0.1.0"


@api_router.get("/health", response_model=HealthResponse)
async def api_health(request: Request) -> HealthResponse:
    """Service health for the interface (`askau-frontend/types/api.ts`).

    Unauthenticated, like the probes: it reports whether dependencies answer,
    never anything about content. `down` is deliberately unreachable from here —
    a request that reaches this handler proves the process is up, so the honest
    values are `ok` and `degraded`. A client that cannot reach us at all sees a
    network error, which is the accurate signal for `down`.
    """
    from datetime import UTC, datetime

    settings = request.app.state.settings
    services: dict[str, str] = {}

    try:
        async with request.app.state.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        services["database"] = "ok"
    except Exception:
        # No exception type in the response: which driver failed and how is
        # operational detail, and this endpoint is unauthenticated.
        services["database"] = "down"

    # Redis loss costs latency, not correctness — the principal resolver falls
    # back to the database — so it degrades rather than downs the service.
    services["redis"] = "ok" if await cache.ping(settings) else "degraded"

    return HealthResponse(
        status="degraded" if any(v != "ok" for v in services.values()) else "ok",
        version=_VERSION,
        timestamp=datetime.now(UTC).isoformat(),
        services=services,
    )
