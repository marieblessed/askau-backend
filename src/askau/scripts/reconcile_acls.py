"""Run the ACL reconciler once, from the command line.

`AclReconciler` closes the gap between `document_acl`, which is authoritative,
and `chunks.acl_principals`, the denormalised copy the authorization predicate
reads (ADR-0002). It was written, tested and then never called by anything —
ADR-0025 recorded that its scheduling was left to whoever operates the
deployment, and in the meantime there was no way to run it at all.

This is that way. It is not a scheduler: it is one pass, on demand, for an
operator who has seen `askau_acl_drift_documents` above zero, or for anyone who
has just changed an access list by hand and wants it effective now rather than
whenever a sweep eventually happens.

**Uses the migration connection deliberately.** Row-level security on `chunks`
restricts `askau_app` to rows overlapping the session principals, and a
maintenance pass sets none — so the reconciler run on the application role reads
zero chunks and reports a clean corpus. It now refuses that rather than lying
(`BlindReconcilerError`), and this script hands it the connection that can
actually see.
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy.ext.asyncio import create_async_engine

from askau.ingestion.acl_reconciler import AclReconciler, BlindReconcilerError
from askau.settings import get_settings


async def main() -> int:
    settings = get_settings()
    engine = create_async_engine(settings.migration_url)
    try:
        reconciler = AclReconciler(engine)

        revocations, grants = await reconciler.find_drift()
        if not revocations and not grants:
            lag = await reconciler.max_lag_seconds()
            print("  nothing to reconcile")
            if lag is not None:
                print(f"  oldest chunk ACL synchronised {lag / 3600:.1f} h ago")
            return 0

        # Reported before the pass, not only after: an operator deciding
        # whether to run this during working hours wants to know the size of it
        # first, and revocations are the half that should not wait.
        print(f"  {len(revocations)} document(s) with an outstanding revocation")
        print(f"  {len(grants)} document(s) with an outstanding grant")

        report = await reconciler.reconcile()
        print(
            f"  reconciled: {report.documents_touched} document(s), "
            f"{report.chunks_updated} chunk(s) rewritten"
        )
        return 0
    except BlindReconcilerError as exc:
        # The one failure worth a distinct exit path. Reported loudly because
        # the alternative — the reconciler silently finding nothing — is what
        # this error exists to prevent.
        print(f"  refused: {exc}", file=sys.stderr)
        return 2
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
