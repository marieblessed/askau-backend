"""The connector port: what the pipeline needs from a repository.

The pipeline was written against `pathlib.Path` — `_discover` globs a directory
and `_one` calls `path.read_bytes()`. That is fine for a filesystem export and
impossible for anything remote, so this is the abstraction that has to exist
before a remote adapter can.

Three things the port carries that a `Path` cannot, and each of them is the
reason a remote source is not simply "a directory somewhere else":

**Per-item access.** This is the whole design. A filesystem export has no
per-file permissions, so every document inherits the source's `access_rules`.
A remote repository's whole point is that two documents in one container can
have different audiences — so `RemoteDocument.principals` is per document, and a
connector that returned the source's list for every item would silently publish
confidential material to everyone who can reach the container. That is the worst
failure this product has, and it is one careless `return source_principals`
away.

**Deferred content.** Enumerating a library is cheap; downloading it is not.
`fetch()` is a callable so the pipeline can skip a document whose content hash
is unchanged without ever transferring it.

**A stable key.** Incremental sync and change detection both need an identifier
that survives a rename. A filename does not; a drive item id does.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class SourcePrincipal:
    """One grantee, as the *source* names it.

    Deliberately the external identifier and not our `principals.id`: a
    connector talks to a repository and must not know about our tables. The
    reconciler resolves these to principal rows, which is also where an
    identifier we have never seen gets created rather than silently dropped.
    """

    #: `user` | `group`. Narrower than `principal_kind`, which also has `role`
    #: and `department` — neither is something a document repository grants to.
    kind: str
    #: The directory's identifier. For Entra this is the object id (a GUID),
    #: never the display name or the UPN: both are mutable, and an ACL keyed on
    #: a mutable field breaks the day somebody marries or changes department.
    external_id: str
    display_name: str = ""


@dataclass(frozen=True, slots=True)
class RemoteDocument:
    """One document as a repository describes it, before any content is fetched."""

    #: Stable within the source, and stable across renames.
    key: str
    title: str
    #: What the reader would open. Vended to the client, never constructed by it.
    uri: str
    #: A filename with an extension, so the extractor can dispatch on type. Not
    #: always the same as `title` — a repository's titles can be set independently.
    filename: str
    #: Who may read this document, per item. An **empty tuple means nobody**,
    #: and the pipeline must treat it that way rather than falling back to the
    #: source's rules. See `AccessUnavailableError` for why that matters.
    principals: tuple[SourcePrincipal, ...] = ()
    #: The document *family* this revision belongs to, when the source names
    #: one. `None` means the key is the family, which is the SharePoint-shaped
    #: assumption: a new version overwrites the same item.
    #:
    #: Blob storage does not have to work that way. A new version is very often
    #: a new blob — `policy-2026.pdf` beside `policy-2025.pdf` — and without
    #: this the two are unrelated documents, both current, both retrievable, and
    #: the superseded one never stops being cited as the rule.
    family_key: str | None = None

    #: The repository's own change token, when it has one. Cheaper than hashing
    #: content we would have to download first.
    #: This document's own classification, when the source states one.
    #:
    #: `None` means the source did not say, and the run's default applies. It
    #: has to be per document: a container is not one classification, `chunks`
    #: is LIST-partitioned on this value, and the clearance check that decides
    #: what a reader may retrieve reads it. Ingesting a mixed library under a
    #: single run-level classification either over-classifies material — hiding
    #: public documents behind a clearance nobody needs — or under-classifies
    #: it, which is the direction that discloses.
    classification: str | None = None
    #: Governance metadata, when the source states it. All optional, all
    #: `None` meaning "the source did not say".
    #:
    #: These are not decoration. `effective_from`/`effective_to` decide whether
    #: a document is current, `version_label` and `doc_type` appear on every
    #: citation, and `department` is how a reader judges whose rule they are
    #: reading. SharePoint had columns for them. **Blob storage has none** — it
    #: has metadata key/values that somebody must populate — so a container
    #: nobody labels produces a corpus of undated, unversioned, unattributed
    #: documents that still answer questions.
    doc_type: str | None = None
    department: str | None = None
    version_label: str | None = None
    effective_from: str | None = None
    effective_to: str | None = None
    etag: str | None = None
    size: int | None = None
    modified_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    #: Set when this document's audience could not be established. Carried on
    #: the document rather than raised out of the enumeration, because raising
    #: aborts the iterator: one anonymously-shared file would end the sync of a
    #: forty-thousand-item library, and every document after it would silently
    #: not be ingested.
    #:
    #: Note the safe interaction with `principals`: a document with an
    #: `access_error` also carries an empty principal set, so a pipeline that
    #: forgot to check this field would store it readable by nobody rather than
    #: readable by everybody. The check exists to *report* the problem, not to
    #: prevent the disclosure — that is already prevented.
    access_error: str | None = None

    #: Downloads the bytes. Separate from enumeration so an unchanged document
    #: costs a comparison rather than a transfer.
    fetch: Callable[[], Awaitable[bytes]] | None = None


class AccessUnavailableError(Exception):
    """The document's permissions could not be determined.

    Raised, not returned, and never caught into a default. A connector that
    cannot read an item's ACL knows one thing for certain: it does not know who
    may see this document. Ingesting it under the source's rules would be a
    guess, and the direction that guess fails in is disclosure.

    So the document is skipped and the run records a failure. An administrator
    then sees "3 documents could not be assessed" rather than three documents
    quietly readable by the wrong people.
    """


class SourceConnector(Protocol):
    """A repository the pipeline can ingest from."""

    @property
    def source_type(self) -> str: ...

    async def probe(self) -> dict[str, Any]:
        """FR-013 reachability check. Returns a verdict; does not raise."""
        ...

    def documents(self) -> AsyncIterator[RemoteDocument]:
        """Enumerate. An async iterator rather than a list because a library of
        forty thousand items should not be materialised to count it."""
        ...

    async def aclose(self) -> None: ...
