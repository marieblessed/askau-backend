"""Wire DTOs. Domain types never reach the network.

The separation matters for one specific reason: ``AuthorizationContext`` holds
the caller's principal set, and a domain object serialized directly would put it
on the wire. The client never needs it, and shipping it would turn a rendering
bug into an information-disclosure bug.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class WireModel(BaseModel):
    """Base for everything that crosses the network.

    Python stays snake_case; the wire is camelCase. The consumer is a TypeScript
    client whose types are hand-written in camelCase and whose whole application
    is built on them (`askau-frontend/types/`), so the boundary converts rather
    than asking every call site on the other side to translate.

    `populate_by_name` keeps snake_case accepted on the way *in*, so a request
    body written either way parses — the API is a published surface and being
    strict about casing on input buys nothing.
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        serialize_by_alias=True,
    )


class ListResponse[T](WireModel):
    """The envelope every collection endpoint returns.

    Shape fixed by the client: `askau-frontend/types/api.ts` declares exactly
    these five fields and its mock router returns them. Note that repo also
    declares a `PaginatedResponse` with `totalPages`/`hasNextPage` which nothing
    uses — the mock is the stronger evidence of what is live, so this is the one
    to match.

    `hasMore` rather than `totalPages` puts the "is there another page" decision
    on the server, where the page size is actually known.
    """

    items: list[T]
    total: int
    page: int = 1
    page_size: int
    has_more: bool = False

    @classmethod
    def of(cls, items: list[T], *, total: int, page: int, page_size: int) -> ListResponse[T]:
        """Build the envelope and derive `hasMore` rather than trusting a caller
        to compute it consistently at each of a dozen call sites."""
        return cls(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            has_more=(page * page_size) < total,
        )


class CitationOut(WireModel):
    marker: int
    document_id: str
    document_title: str
    source_name: str
    section_ref: str | None = None
    page_from: int | None = None
    page_to: int | None = None
    version_label: str | None = None
    effective_from: date | None = None
    quote: str | None = None
    can_open: bool = True
    #: public | internal | confidential | highly_restricted. The interface uses
    #: this to mark how the answer must be handled, not to decide access —
    #: access was settled before retrieval ran.
    classification: str = "internal"


class AskRequest(WireModel):
    """A question. Note what is *not* here: any retrieval parameter.

    `candidate_k` is how much work the database is asked to do per question.
    A client that can set it can ask for 500 candidates on every request, and
    no validated upper bound makes that a sensible thing to expose to a
    browser. Retrieval width is chosen server-side from a named profile
    (`domain/profiles.py`); the reader's influence over it is a consent flag on
    their account, not a number in a request body.

    `extra="forbid"` so sending one is a 422 rather than a silent no-op. The
    difference matters: ignoring `{"topK": 100}` leaves an integrator believing
    it worked and quietly getting different results than they think, which
    surfaces as a bug report about relevance months later.
    """

    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=2000)
    include_historical: bool = False
    language: str = "en"


class AnswerOut(WireModel):
    """A live answer.

    Carries **both** state fields, exactly as `MessageOut` does, and that
    symmetry is the point rather than redundancy.

    `state` was missing here for a while and the consequence was not subtle:
    their `AIMessage` is a chain of `message.state === "x" &&` blocks with no
    default branch, so a live answer arrived with nothing to match and rendered
    an empty message — while the *same turn*, re-read from the transcript,
    rendered correctly, because `stored_message_to_wire` did supply it. One
    answer, two endpoints, two different shapes.
    """

    message_id: str | None = None
    #: One of the five the client can render. Same derivation as
    #: `MessageOut.state`, including the promotion to `outdated` when the answer
    #: rests on superseded material — otherwise a live answer says `grounded`
    #: and the same turn says `outdated` after a refresh.
    state: str
    #: Our full state, unmapped, alongside it.
    answer_state: str
    content: str
    #: The citations in the shape `features/chat` renders. Present for the same
    #: reason `state` is: `MessageOut` supplied these and the live response did
    #: not, so a turn read back from the transcript had source cards and the
    #: same turn, live, had none — the client would have had to map
    #: `CitationOut` itself, which is the mapping this boundary exists to do.
    sources: list[SourceOut] = []
    #: Rendered as "Grounded in N approved sources". A count, not a score.
    grounding_count: int | None = None
    conflicting_sources: list[SourceOut] = []
    #: The flatter shape, kept for the evaluation harness and `/debug/retrieve`,
    #: which want the marker and the document id rather than a card.
    citations: list[CitationOut] = []
    conflicts: list[str] = []
    groundedness: float | None = None
    correlation_id: str
    timings: dict[str, int] = {}


class RetrievedChunkOut(WireModel):
    chunk_id: int
    document_id: str
    document_title: str
    classification: str
    section_ref: str | None = None
    page_from: int | None = None
    score: float
    matched_both_arms: bool
    excerpt: str


class DebugRetrieveOut(WireModel):
    question: str
    strategy: str
    took_ms: int
    count: int
    chunks: list[RetrievedChunkOut]


class MeOut(WireModel):
    user_id: str
    email: str | None
    department: str | None
    roles: list[str]
    #: A summary, never the principal set itself.
    max_classification: str
    principal_count: int


class HealthOut(WireModel):
    status: Literal["ok", "degraded", "error"]
    checks: dict[str, str] = {}


# ── shapes fixed by askau-frontend ──────────────────────────────────────────
#
# Everything below mirrors a hand-written TypeScript type in that repo. Where a
# name here looks unlike the rest of this codebase, that is why — the client's
# types are what its whole application is built on, and this is the boundary
# that converts.


class SourceOut(WireModel):
    """A citation, in the shape `features/chat` actually renders.

    Their repo contains two models of this and they disagree: `types/citation.ts`
    is labelled the API contract, but `features/chat/types/index.ts` is what the
    components consume. This is the second one, because a contract the UI cannot
    render is not a contract.

    Their `ChatResponse` returns only `citationIds`, implying a citation-fetch
    endpoint that exists nowhere in their code. We return sources inline instead:
    one round trip, and no window in which an answer is on screen without the
    provenance that justifies it.
    """

    id: str
    title: str
    section: str = ""
    page: int = 0
    version: str = ""
    #: Year only — their card renders it as a bare "2026".
    published: str = ""
    #: PUBLIC | INTERNAL | CONFIDENTIAL | HIGHLY_RESTRICTED. Upper-cased at the
    #: boundary; the enum is lower-case everywhere inside.
    classification: str = "INTERNAL"
    department: str = ""
    doc_type: str = "other"
    effective_date: str = ""
    #: Display text for the document's lifecycle — "Active", "Superseded".
    status: str = ""
    excerpt: str = ""
    #: Their `types/citation.ts` name for our `can_open`. Grounding and opening
    #: are separate permissions (FR-031): a source may support an answer that the
    #: reader is not allowed to open.
    has_access: bool = True
    #: Vended by us, never constructed by the client — their own type comment
    #: says "The frontend must never attempt to construct direct document URLs."
    access_url: str | None = None
    #: 1-based, matching the marker rendered inline in the prose.
    citation_index: int = 1


class MessageOut(WireModel):
    """One turn. Mirrors `features/chat/types/index.ts`.

    `state`, `sources` and `groundingCount` are columns and joins, not values
    inferred by the client — the client must never be in the position of deciding
    whether an answer was grounded.
    """

    id: str
    conversation_id: str
    role: Literal["user", "assistant"]
    content: str
    #: One of the five the client can render: grounded | insufficient | outdated
    #: | conflicting | error. Never anything else — their `AIMessage` has no
    #: default branch, so an unknown value renders an empty message.
    state: str | None = None
    #: Our full state, unmapped. Carried so the finer distinctions — a
    #: clarification, a safety refusal, an out-of-scope question — are on the
    #: wire and available the moment their UI grows a branch for them. Today
    #: `state` collapses all three into `insufficient`.
    answer_state: str | None = None
    sources: list[SourceOut] = []
    conflicting_sources: list[SourceOut] = []
    grounding_count: int | None = None
    retrieved_at: str | None = None
    feedback: str | None = None
    not_helpful_reason: str | None = None
    created_at: str


class ConversationOut(WireModel):
    """Mirrors `types/conversation.ts`, plus the two fields their sidebar and
    list both display but the type omits."""

    id: str
    user_id: str
    title: str | None = None
    #: active | archived | deleted. Their type is tri-state where ours was a
    #: boolean; widened rather than mapped, because "deleted" is a real state
    #: their menu offers.
    status: str = "active"
    message_count: int = 0
    last_message_preview: str | None = None
    created_at: str
    updated_at: str


class ConversationDetailOut(ConversationOut):
    messages: list[MessageOut] = []


class UserOut(WireModel):
    """Mirrors `types/auth.ts`.

    `roles` is deliberately the client's three-tier vocabulary
    (`user | admin | super_admin`), not ours. Ours separates knowledge, system
    and security administration so no single account can both change the corpus
    and erase the record of doing so (§6.4) — that separation stays server-side
    and keeps being enforced. The client only needs to know whether to show an
    admin affordance, and its own code annotates every role check as UX-only
    with the backend authoritative.
    """

    id: str
    email: str
    name: str
    display_name: str
    entra_id: str | None = None
    roles: list[str] = []
    department: str | None = None
    job_title: str | None = None
    language_preference: str | None = None
    created_at: str | None = None
    last_login_at: str | None = None
    #: Ours, not theirs: the highest classification this person can reach. Kept
    #: because the interface uses it to explain why an answer came back thin.
    max_classification: str = "public"


class PreferencesOut(WireModel):
    """What the settings modal can actually change.

    Deliberately two fields and not six. The modal offers session timeout, MFA,
    notification toggles and a retrieval-depth switch as well; those are either
    the identity platform's to answer or genuinely unbuilt, and returning them
    here would imply otherwise.
    """

    save_history: bool = True
    share_analytics: bool = True
    #: Consent to escalate retrieval when the first pass is weak. Off by
    #: default — the other two are opt-outs of useful behaviour, this is an
    #: opt-in to extra work. Note it is *consent*, not a tier: the client
    #: cannot choose `thorough`, only permit it.
    higher_intelligence: bool = False


class PreferencesIn(WireModel):
    """Both optional: the modal changes one toggle at a time, and a PATCH that
    silently reset the other would be a privacy setting turning itself back on."""

    save_history: bool | None = None
    share_analytics: bool | None = None
    higher_intelligence: bool | None = None


class KnowledgeBaseOut(WireModel):
    """One repository, as the settings modal lists it.

    `documentCount` is scoped to the caller — see the route. No `version`
    field: their UI renders one and `knowledge_sources` has no such column.
    """

    id: str
    name: str
    department: str
    source_type: str
    #: Always "active" today; the registry lists nothing else. Carried anyway
    #: so their status pill has a value to render rather than a hardcoded one.
    status: str
    document_count: int
    last_synced_at: str | None = None
    last_sync_status: str | None = None


class DeletionOut(WireModel):
    deleted: int


class HealthResponse(WireModel):
    """Mirrors `types/api.ts`. Distinct from `HealthOut`, which serves the
    kubelet probes — this one serves the interface, and the two have different
    audiences and different failure semantics."""

    status: Literal["ok", "degraded", "down"]
    version: str
    timestamp: str
    services: dict[str, str] | None = None


# ── streaming events ────────────────────────────────────────────────────────
#
# The client's `lib/api/client.ts` has a prepared `stream()` that POSTs with
# `Accept: text/event-stream` and hands back the raw body — but nothing consumes
# it, so no event name or payload is defined on their side. These models *are*
# that definition. They exist as models rather than inline dicts for one
# practical reason: the payloads went camelCase with everything else, and a
# hand-built dict is where a stray `answer_state` survives unnoticed.
#
# SSE payloads do not appear in OpenAPI, so this module is the contract for them.


class AcceptedEvent(WireModel):
    """First frame. Carries the correlation id before any work begins, so a
    request that later fails is still traceable from the client's side."""

    correlation_id: str


class StageEvent(WireModel):
    """Retrieval progress. Their `LoadingMessage` renders three named stages
    with checkmarks — this is what drives them, rather than the `setTimeout`
    chain it currently fakes."""

    stage: str
    elapsed_ms: int


class TokenEvent(WireModel):
    text: str


class SourcesEvent(WireModel):
    """Sent once, before generation. The reader sees what the answer will be
    built from while it is still being written."""

    #: The renderable shape, so the panel can be populated from this frame
    #: directly rather than waiting for `done`.
    sources: list[SourceOut] = []
    citations: list[CitationOut] = []


class ConflictEvent(WireModel):
    summary: str


class DoneEvent(WireModel):
    """Terminal frame for a successful turn.

    `content` is here as well as in the token stream because a refusal emits no
    tokens at all: FR-028 requires AskAU to *state* that it cannot answer, and a
    bare status label is not that statement.

    Carries `state` for the same reason `AnswerOut` does: the streaming path
    must not describe a turn differently from the buffered one, or which
    endpoint the client happened to use decides whether the answer renders.
    """

    #: The client-renderable state. See `AnswerOut.state`.
    state: str
    answer_state: str
    content: str
    #: Same shape and same reason as `AnswerOut.sources`.
    sources: list[SourceOut] = []
    grounding_count: int | None = None
    topics: list[str] = []
    groundedness: float | None = None
    citations: list[CitationOut] = []
    message_id: str | None = None


class ErrorEvent(WireModel):
    """Terminal frame for a failed turn.

    Only the exception's type name, never its message — a stack or a database
    error string on the wire is an information disclosure. The correlation id is
    what makes it diagnosable.
    """

    type: str
    correlation_id: str
