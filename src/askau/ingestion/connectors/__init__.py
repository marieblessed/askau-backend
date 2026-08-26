"""Knowledge source connectors.

A port with adapters, so a new repository type is one file rather than a change
to the pipeline. `probe` is the FR-013 pre-activation check: it answers "can we
reach this?" before an administrator activates a source and discovers the answer
as a run full of errors.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


async def probe(source_type: str, location: dict[str, Any]) -> dict[str, Any]:
    """Check reachability without ingesting anything.

    Returns a verdict rather than raising: "this source is unreachable" is a
    normal answer to a connection test, not an exception.
    """
    match source_type:
        case "filesystem":
            return _probe_filesystem(location)
        case "manual":
            return {
                "ok": True,
                "detail": "Manual sources accept direct uploads; nothing to reach.",
            }
        case "sharepoint" | "dms" | "s3" | "http":
            # The adapters are ports without implementations in Phase 1 — AUC
            # tenant access is a dependency, not a coding task. Saying so is
            # better than a green tick that means nothing.
            return {
                "ok": False,
                "detail": (
                    f"The {source_type} connector is not implemented in this "
                    "release. Register the source and use a filesystem export, "
                    "or wait for Phase 2 integration."
                ),
                "reason": "connector_not_implemented",
            }
        case _:
            return {"ok": False, "detail": f"Unknown source type {source_type!r}"}


def _probe_filesystem(location: dict[str, Any]) -> dict[str, Any]:
    raw = location.get("path")
    if not raw:
        return {"ok": False, "detail": "location.path is required", "reason": "missing_path"}

    path = Path(str(raw)).expanduser()
    if not path.exists():
        return {"ok": False, "detail": f"{path} does not exist", "reason": "not_found"}
    if not path.is_dir():
        return {"ok": False, "detail": f"{path} is not a directory", "reason": "not_a_directory"}

    supported = {".pdf", ".docx", ".xlsx", ".pptx", ".html", ".htm", ".txt", ".md"}
    files = [p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in supported]
    if not files:
        return {
            "ok": True,
            "detail": f"{path} is reachable but contains no supported documents",
            "documents_found": 0,
            "reason": "empty",
        }
    return {
        "ok": True,
        "detail": f"{len(files)} supported document(s) found",
        "documents_found": len(files),
    }
