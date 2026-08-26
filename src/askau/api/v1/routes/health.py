"""Health endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import text

from askau.api.schemas.wire import HealthOut
from askau.core import cache

router = APIRouter(tags=["health"])


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
