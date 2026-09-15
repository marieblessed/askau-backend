"""Source connectors.

Implemented: `filesystem`, `manual`, `azure_blob`.

The AUC settled on **Azure Blob Storage** for documents and **Entra ID** for
identity (ADR-0029). SharePoint, OneDrive, S3 and the generic web connector were
removed rather than kept as options: an adapter nobody will deploy is code that
must still compile, typecheck, be tested and be read, and the one that mattered
— SharePoint's per-item permission mapping — survives as prose in that ADR and
in git history rather than as a module pretending to be reachable.

`filesystem` and `manual` stay. Neither connects to another system: one reads a
local export, the other accepts direct uploads, and both are how the corpus is
exercised in development and in tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from askau.ingestion.connectors.azure_blob import AzureBlobConnector
from askau.ingestion.connectors.azure_storage import AzureStorageClient
from askau.ingestion.connectors.ports import (
    AccessUnavailableError,
    RemoteDocument,
    SourceConnector,
    SourcePrincipal,
)
from askau.settings import Settings

__all__ = [
    "AccessUnavailableError",
    "AzureBlobConnector",
    "RemoteDocument",
    "SourceConnector",
    "SourcePrincipal",
    "build",
    "probe",
]


def build(source_type: str, location: dict[str, Any], settings: Settings) -> SourceConnector | None:
    """The adapter for a source type, or `None` where there is not one.

    `None` rather than a raise: an unconfigured source is a state the deployment
    is in, not an error in the caller. `probe` turns it into a verdict an
    administrator can read.
    """
    if source_type == "azure_blob":
        if not settings.azure_storage_configured:
            return None
        return AzureBlobConnector(
            location,
            AzureStorageClient(
                account_url=str(location.get("account_url") or ""),
                tenant_id=settings.entra_tenant_id,
                client_id=settings.azure_storage_client_id,
                client_secret=settings.azure_storage_client_secret,
                sas_token=settings.azure_storage_sas_token,
            ),
        )
    return None


async def probe(
    source_type: str, location: dict[str, Any], settings: Settings | None = None
) -> dict[str, Any]:
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
        case "azure_blob":
            if settings is None:
                return {
                    "ok": False,
                    "detail": "Probing Azure Blob Storage requires server configuration.",
                    "reason": "not_configured",
                }
            connector = build("azure_blob", location, settings)
            if connector is None:
                return {
                    "ok": False,
                    "detail": (
                        "Azure Blob Storage is not configured. Set "
                        "ASKAU_AZURE_STORAGE_CLIENT_ID and ASKAU_AZURE_STORAGE_CLIENT_SECRET "
                        "(or ASKAU_AZURE_STORAGE_SAS_TOKEN), and give the application "
                        "Storage Blob Data Reader on the container."
                    ),
                    "reason": "not_configured",
                }
            try:
                return await connector.probe()
            finally:
                await connector.aclose()
        case _:
            return {
                "ok": False,
                "detail": (
                    f"'{source_type}' is not a source type this deployment supports. "
                    "Documents come from Azure Blob Storage; `filesystem` and `manual` "
                    "exist for development."
                ),
                "reason": "unsupported",
            }


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
        }
    return {
        "ok": True,
        "detail": f"{len(files)} supported document(s) found",
        "documents_found": len(files),
    }
