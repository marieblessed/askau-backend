"""Azure Blob Storage, the AUC's chosen document store.

Replaces SharePoint as the ingestion target (ADR-0029). The SharePoint
connector stays as the reference for per-item permission mapping, but new work
happens here.

## The problem this connector has to solve, and SharePoint did not

SharePoint answers "who may read this document?" — that is what a document
management system is for. **Blob storage does not.** Access is granted at the
container by RBAC or a SAS, and every blob inside it is equally reachable by
whoever holds that grant. There is no per-blob audience to read.

That matters more here than anywhere else in the system, because the product's
whole claim is that two people asking the same question get different answers.
Ingest a flat container under one grant and that claim quietly becomes false —
not with an error, but with every reader seeing everything.

So the audience has to come from somewhere, and this connector makes that an
explicit, configured choice rather than a default:

* **`metadata`** — blob metadata carries a delimited list of Entra object ids.
  Per-document control, set by whoever uploads.
* **`prefix`** — the blob's path, matched against `prefix_map`. A folder
  convention: `hr/` grants the HR group.
* **`fixed`** — one list for every blob, for a container with one audience.

`prefix` is the one to reach for by default. An administrator can see the whole
access model by looking at the tree, it needs nothing of the person uploading,
and it maps onto how organisations already arrange shared drives.

## Fail closed, and the mode that fails closed hardest

`metadata` is the strict one: a blob with no audience metadata is **not
ingested**. It carries `access_error`, the run reports it, and an administrator
sees "4 documents could not be assessed". The alternative — treating absent
metadata as "everyone" — is one line away and is the disclosure this design
exists to prevent.

`prefix` is the same: a blob under no configured prefix matches nothing, and an
unmatched blob is reported rather than published. `prefix_map` may name a
`"*"` fallback, but it has to be written down deliberately.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import date
from typing import Any

from askau.ingestion.connectors.azure_storage import AzureStorageClient, AzureStorageError
from askau.ingestion.connectors.ports import RemoteDocument, SourcePrincipal

_log = logging.getLogger(__name__)

#: Extensions the extractors handle. Filtered during enumeration — a container
#: legitimately holds images and archives nobody asked us to read.
_SUPPORTED = (".pdf", ".docx", ".xlsx", ".pptx", ".html", ".htm", ".txt", ".md")

#: Default metadata key carrying the audience. Blob metadata keys are C#
#: identifiers — letters, digits and underscore — so no hyphens here.
_DEFAULT_METADATA_KEY = "askau_principals"

#: Metadata key carrying the document's own classification. Independent of the
#: ACL strategy — a `prefix`-mapped container still has documents of differing
#: sensitivity, and the two questions ("who may read it" and "how sensitive is
#: it") are answered separately in this product and should be here too.
_CLASSIFICATION_KEY = "askau_classification"

#: Governance metadata keys. Blob metadata keys are case-insensitive on the wire
#: and arrive lowercased from the listing, so they are compared lowercased here.
_FAMILY_KEY = "askau_family"

_GOVERNANCE_KEYS = {
    "doc_type": "askau_doc_type",
    "department": "askau_department",
    "version_label": "askau_version",
    "effective_from": "askau_effective_from",
    "effective_to": "askau_effective_to",
}


def _is_date(value: str) -> bool:
    """A real calendar date, not a string of the right shape.

    This was a regex, which accepts `2026-02-31` — a date that does not exist,
    passes validation, and then fails at the database mid-run.

    Present-but-unparseable is refused rather than dropped. Dropping looks
    harmless and is not: `effective_to` is what makes a document expired, and a
    malformed one silently becoming NULL means "no expiry" — a withdrawn policy
    stays live and keeps being cited as current.
    """
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


_STRATEGIES = ("metadata", "prefix", "fixed")


class AzureBlobConnector:
    """One container, or one prefix within it.

    `location` shape::

        {"account_url": "https://auc.blob.core.windows.net",
         "container": "policies",
         "prefix": "current/",                  # optional
         "acl_strategy": "prefix",
         "prefix_map": {"current/hr/": ["<entra-group-oid>"],
                        "current/finance/": ["<entra-group-oid>"]},
         "metadata_key": "askau_principals",    # metadata strategy only
         "fixed_principals": ["<oid>"],         # fixed strategy only
         "max_documents": 500}
    """

    source_type = "azure_blob"

    def __init__(self, location: dict[str, Any], client: AzureStorageClient) -> None:
        self._account_url = str(location.get("account_url") or "").strip().rstrip("/")
        self._container = str(location.get("container") or "").strip()
        self._prefix = str(location.get("prefix") or "").lstrip("/")
        self._strategy = str(location.get("acl_strategy") or "").strip()
        self._prefix_map: dict[str, list[str]] = dict(location.get("prefix_map") or {})
        self._metadata_key = str(location.get("metadata_key") or _DEFAULT_METADATA_KEY).lower()
        self._classification_key = str(
            location.get("classification_key") or _CLASSIFICATION_KEY
        ).lower()
        self._fixed = [str(x) for x in (location.get("fixed_principals") or ())]
        self._max_documents = int(location.get("max_documents") or 1000)
        self._client = client

    # ── audience ────────────────────────────────────────────────────────────

    def _principals_for(
        self, name: str, metadata: dict[str, str] | None
    ) -> tuple[tuple[SourcePrincipal, ...], str | None]:
        """This blob's audience, or the reason it could not be determined.

        Returns `(principals, access_error)`. A non-None error means the
        document is reported and not ingested; the principals are empty in that
        case as well, so a caller that ignored the error would store it readable
        by nobody rather than by everybody.
        """
        if self._strategy == "fixed":
            return self._as_principals(self._fixed), None

        if self._strategy == "metadata":
            raw = (metadata or {}).get(self._metadata_key, "").strip()
            if not raw:
                return (), (
                    f"no '{self._metadata_key}' metadata on this blob, so its audience is "
                    "unknown. Set it to a comma-separated list of Entra object ids."
                )
            ids = [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]
            if not ids:
                return (), (f"'{self._metadata_key}' is present but names no principal.")
            return self._as_principals(ids), None

        if self._strategy == "prefix":
            # Longest match wins, so `hr/confidential/` can be narrower than
            # `hr/`. Sorted by length rather than relying on dict order, which
            # would make the access model depend on how the JSON was typed.
            for pattern in sorted(self._prefix_map, key=len, reverse=True):
                if pattern != "*" and name.startswith(pattern):
                    return self._as_principals(self._prefix_map[pattern]), None
            if "*" in self._prefix_map:
                return self._as_principals(self._prefix_map["*"]), None
            return (), (
                f"'{name}' is under no configured prefix, so its audience is unknown. "
                "Add a prefix_map entry for it, or a '*' fallback."
            )

        return (), f"unknown acl_strategy '{self._strategy}'"

    def _governance(self, metadata: dict[str, str]) -> tuple[dict[str, str | None], str | None]:
        """Governance metadata, and the reason it could not be trusted.

        A bad date returns an error so the document is reported rather than
        ingested with a silently absent expiry.
        """
        out: dict[str, str | None] = {}
        for field, key in _GOVERNANCE_KEYS.items():
            value = (metadata.get(key) or "").strip()
            if not value:
                out[field] = None
                continue
            if field.startswith("effective_") and not _is_date(value):
                return {}, (
                    f"'{key}' is '{value}', which is not a real YYYY-MM-DD date. A document "
                    "whose validity window cannot be read must not be presented as current."
                )
            out[field] = value
        return out, None

    @staticmethod
    def _as_principals(ids: list[str]) -> tuple[SourcePrincipal, ...]:
        # `group` because an Entra object id in an access list is a group in
        # practice. A person granted access individually is expressed the same
        # way from the source's side; the reconciler resolves either.
        return tuple(SourcePrincipal(kind="group", external_id=i, display_name="") for i in ids)

    # ── the port ────────────────────────────────────────────────────────────

    def _configuration_error(self) -> str | None:
        if not self._container:
            return "location.container is required"
        if not self._account_url:
            return "location.account_url is required"
        if self._strategy not in _STRATEGIES:
            return (
                f"location.acl_strategy must be one of {', '.join(_STRATEGIES)} — blob "
                "storage has no per-blob permissions, so the audience has to be stated. "
                "See the module docstring."
            )
        if self._strategy == "fixed" and not self._fixed:
            return "acl_strategy 'fixed' needs location.fixed_principals"
        if self._strategy == "prefix" and not self._prefix_map:
            return "acl_strategy 'prefix' needs location.prefix_map"
        return None

    async def probe(self) -> dict[str, Any]:
        """FR-013. A verdict, never an exception."""
        problem = self._configuration_error()
        if problem:
            return {"ok": False, "detail": problem, "reason": "misconfigured"}

        try:
            blobs = await self._client.list_blobs(self._container, prefix=self._prefix)
        except AzureStorageError as exc:
            return {
                "ok": False,
                "detail": f"Could not reach the container: {exc.detail}",
                "reason": "unreachable",
            }
        except Exception as exc:  # network, DNS, TLS
            return {
                "ok": False,
                "detail": f"Could not reach the container: {type(exc).__name__}: {exc}",
                "reason": "unreachable",
            }

        supported = [b for b in blobs if str(b["name"]).lower().endswith(_SUPPORTED)]

        # Reported, because it is the misconfiguration an administrator is most
        # likely to have made and least likely to notice: the container is
        # reachable, the documents are there, and every one of them would be
        # skipped for want of an audience.
        unassignable = sum(
            1 for b in supported if self._principals_for(str(b["name"]), b["metadata"])[1]
        )
        detail = f"{len(supported)} supported document(s) in {self._container}/{self._prefix}"
        if unassignable:
            detail += f"; {unassignable} with no determinable audience under '{self._strategy}'"

        return {"ok": True, "detail": detail, "documents_found": len(supported)}

    async def documents(self) -> AsyncIterator[RemoteDocument]:
        emitted = 0
        for blob in await self._client.list_blobs(
            self._container, prefix=self._prefix, include_metadata=True
        ):
            name = str(blob["name"])
            if not name.lower().endswith(_SUPPORTED):
                continue
            if emitted >= self._max_documents:
                return
            emitted += 1

            principals, access_error = self._principals_for(name, blob["metadata"])
            filename = name.rsplit("/", 1)[-1]
            # Absent means "the source did not say", and the run's default
            # applies. Present but wrong is refused by the pipeline rather than
            # quietly downgraded — see `_ingest_one`.
            declared = (blob["metadata"] or {}).get(self._classification_key, "").strip().lower()
            governance, bad_date = self._governance(blob["metadata"] or {})
            if bad_date and not access_error:
                access_error = bad_date
            yield RemoteDocument(
                # The blob name: stable across content changes, which an etag is
                # not, and incremental sync keys on it.
                key=name,
                # Absent means the blob is its own family, which is right when
                # a new version overwrites the file. Present, several blobs can
                # be revisions of one document.
                family_key=(blob["metadata"] or {}).get(_FAMILY_KEY, "").strip() or None,
                title=filename.rsplit(".", 1)[0],
                uri=self._client.blob_url(self._container, name),
                filename=filename,
                principals=principals,
                classification=declared or None,
                # Named rather than splatted: `**governance` typechecks as
                # `dict[str, Any]` against `extra`, so a misspelled key would
                # land silently in the wrong field instead of failing here.
                doc_type=governance.get("doc_type"),
                department=governance.get("department"),
                version_label=governance.get("version_label"),
                effective_from=governance.get("effective_from"),
                effective_to=governance.get("effective_to"),
                access_error=access_error,
                etag=blob["etag"] or None,
                size=blob["size"],
                modified_at=blob["last_modified"] or None,
                fetch=self._fetcher(name),
            )

    def _fetcher(self, name: str):  # type: ignore[no-untyped-def]
        """A closure per blob — a factory, so the loop variable is not captured
        by reference and every document does not download the last one."""

        async def fetch() -> bytes:
            return await self._client.download(self._container, name)

        return fetch

    async def aclose(self) -> None:
        await self._client.aclose()
