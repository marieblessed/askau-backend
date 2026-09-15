# AskAU — UI Architecture (Phase 1)

Next.js 15 App Router. Two distinct products in one codebase: the **conversational
assistant** for faculty and staff (SRS §3.1) and the **administration console** for
knowledge/system/security admins (§2.5, kept separate from the end-user interface).

## 1. Rendering strategy

| Surface | Strategy | Why |
|---|---|---|
| App shell, sidebar, conversation list | React Server Component | Session and conversation list resolve on the server; first paint arrives with real content, no client-side auth round trip |
| Conversation history | RSC, streamed | Long histories render progressively |
| Live answer | Client Component + SSE | Token streaming needs a client boundary; it is the *only* one on the read path |
| Composer | Client | Input state |
| Admin tables | RSC + server actions | Data-dense, low-interaction; no need to ship a client data layer |
| Admin charts | Client, lazy | Chart runtime is loaded only on the routes that use it |

The client bundle carries the composer, the stream reader, the citation drawer, and
the feedback control — nothing else. Everything that can render on the server does.

## 2. Route map

```
app/
├── (auth)/
│   └── sign-in/                      Entra redirect; no credential form ever rendered
│
├── (chat)/                           ── end-user product ──
│   ├── layout.tsx                    RSC shell: sidebar + conversation list + user menu
│   ├── page.tsx                      New conversation: empty state + suggested questions
│   └── c/[id]/page.tsx               Conversation: RSC history, client stream for the live turn
│
├── (admin)/admin/                    ── administration console ──
│   ├── layout.tsx                    Role-gated nav (knowledge / system / security)
│   ├── page.tsx                      Overview: health tiles + ingestion + activity   (FR-048)
│   ├── sources/                      List · new · [id] detail/edit · runs             (FR-011, FR-049)
│   ├── documents/                    Filterable table · [id] detail · versions        (FR-046, FR-047)
│   ├── ingestion/                    Run history · [runId] per-document errors        (FR-050)
│   ├── feedback/                     Negative-feedback triage queue                   (FR-044)
│   ├── quality/                      Groundedness, citation accuracy, refusal rate    (FR-054)
│   ├── usage/                        Tokens by operation · model · directorate       (FR-053, §7.4)
│   ├── audit/                        Audit search + export           (security_admin)  (FR-051)
│   └── evaluation/                   Eval runs + regression compare                   (§7.5)
│
└── api/auth/[...oidc]/route.ts       OIDC handshake → httpOnly, SameSite=Lax cookie
```

Route groups, not just folders: `(chat)` and `(admin)` have different layouts,
different navigation, and different role gates. `middleware.ts` rejects unauthenticated
requests at the edge and `(admin)/layout.tsx` re-checks role server-side — the client
never decides whether the admin console renders.

## 3. Component architecture — chat

```
(chat)/c/[id]/page.tsx                       [RSC]
└── ConversationView                          [client boundary]
    ├── MessageList                           [RSC for history]
    │   ├── UserMessage
    │   └── AssistantMessage
    │       ├── AnswerStateBanner             ← FR-028/034/035/009
    │       ├── AnswerBody                    markdown: paragraphs, lists, steps, tables (FR-032)
    │       │   └── CitationChip [1] [2]      inline, click → drawer
    │       ├── ConflictNotice                ← FR-035
    │       ├── SourceList                    title · §/page · version · effective date
    │       └── FeedbackBar                   ← FR-043/044
    ├── StreamingAnswer                       [client] active turn only
    │   ├── StageIndicator                    understanding → retrieving → composing
    │   ├── SourcePreviewStrip                renders on `sources`, before tokens
    │   └── TokenStream
    ├── Composer                              [client] textarea, submit, stop
    └── CitationDrawer                        [client] side panel
        ├── DocumentHeader                    title · source · owner · version · effective window
        ├── QuotedSpan                        exact supporting text
        ├── OpenAuthoritativeButton           → /documents/{id}/open  (FR-031, BR-004)
        └── UnavailableNote                   when can_open === false
```

### The three UI decisions that carry the product

**1. `answer_state` drives a distinct visual treatment, not a tone shift.**

| State | Treatment |
|---|---|
| `grounded` | Normal answer + citations |
| `partially_grounded` | Answer + "parts of this could not be verified against AUC sources" |
| `conflict` | Amber notice above the answer, both sources side by side with dates (FR-035) |
| `insufficient_evidence` | No prose answer at all. A clear statement + "what you can try" + the closest sources found (FR-028) |
| `out_of_scope` | "This is outside AUC knowledge sources" (FR-009) |
| `clarification_needed` | The clarifying question as the primary content, with quick-reply chips (FR-008) |
| `refused_safety` | Neutral refusal, no detail on the trigger |
| `error` | Correlation ID + retry. Never a fabricated answer (§2.3 degraded mode) |

An honest refusal rendered as a *confident, well-designed component* reads as
competence. A refusal rendered as greyed-out apologetic text reads as failure. The
system refuses by design (FR-028), so refusal is a designed state — this is where user
trust in an enterprise RAG product is won or lost.

**2. Citations are load-bearing UI, not footnotes.** Sources appear *before* the prose
(from the `sources` SSE event), inline chips are clickable at the exact claim, and the
drawer shows the verbatim supporting span next to the document's version and effective
date. The user's real task is not "read an answer" — it is "act on policy with
confidence," which requires seeing the authority behind each sentence. Unverified
citations are stripped server-side, so anything rendered is guaranteed real (FR-030).

**3. The wait is narrated, not spinnered.** `StageIndicator` shows understanding →
retrieving → composing, then `SourcePreviewStrip` shows the documents found. By the
time tokens arrive (~600 ms) the user has already seen two units of real progress.
Perceived latency is a product surface, and NFR-002a's 3 s budget is much easier to
live inside when the first 600 ms is informative.

## 4. State management

Deliberately minimal — no Redux/Zustand store.

| State | Owner |
|---|---|
| Session / user / roles | Server (RSC + httpOnly cookie); never in client state |
| Conversation list | RSC, revalidated by tag on mutation |
| Message history | RSC, streamed |
| Active stream | `useStreamingAnswer` — a `useReducer` over SSE events |
| Composer draft | Local `useState` + `sessionStorage` (survives reload) |
| Citation drawer | URL search param (`?cite=`) — shareable, back-button correct |
| Optimistic user message | `useOptimistic` |

`useStreamingAnswer` is the one non-trivial hook: a reducer whose actions are exactly
the SSE event types. This makes stream handling exhaustively type-checked — a new
event type is a compile error until the UI handles it, which is how the `conflict` and
`insufficient_evidence` paths avoid silently degrading to a blank answer.

```ts
type StreamEvent =
  | { type: 'accepted';  messageId: string; correlationId: string }
  | { type: 'stage';     stage: 'understanding' | 'retrieving' | 'composing' }
  | { type: 'sources';   citations: CitationPreview[] }
  | { type: 'token';     text: string }
  | { type: 'conflict';  summary: string; documents: ConflictRef[] }
  | { type: 'done';      answerState: AnswerState; groundedness: number; timings: Timings }
  | { type: 'error';     errorType: string; correlationId: string };
```

Types are generated from `/openapi.json` at build time, so a backend contract change
breaks `npm run typecheck` rather than production.

## 5. Performance budget

| Metric | Budget | Mechanism |
|---|---|---|
| LCP (chat) | < 1.2 s | RSC shell, no client auth round trip, `next/font` self-hosted |
| Client JS (chat route) | < 120 KB gz | Server-first; Radix primitives only; charts excluded from this route |
| INP | < 100 ms | Virtualized message list past 50 turns; token batching at ~30 ms frames |
| First visible progress | < 250 ms | `accepted`/`stage` events |
| First source shown | < 500 ms | `sources` event precedes tokens |
| CLS | < 0.05 | Reserved height for the source strip and banner |

Token batching matters more than it sounds: naïvely setting state per SSE token
re-renders the markdown tree hundreds of times a second. Batching into ~30 ms frames
keeps INP inside budget on the low-spec laptops that are realistic for AUC staff.

## 6. Devices and browsers

SRS §3.2 requires access from standard AUC desktop and laptop computers and, *where
authorized*, mobile devices — via a supported web browser. Neither the device range nor
the browser set was defined, so this is the proposed baseline.

| Target | Support | Notes |
|---|---|---|
| Desktop / laptop | Full, primary | The assumed working context |
| Tablet | Full | Same layout as desktop below 1024 px, sidebar collapses |
| Phone | **Full chat, read-only admin** | See below |
| Chrome, Edge | Last 2 major versions | Edge is the likely AUC standard alongside Entra |
| Firefox, Safari | Last 2 major versions | |
| Internet Explorer | Not supported | |

**Mobile gets the full chat experience and a deliberately reduced admin console.** Asking
a question, reading a grounded answer, and opening a citation all work on a phone — that
is the primary user class (§2.5) and the questions AskAU answers ("what is the travel
procedure?") are exactly the ones people ask away from a desk.

Administration is different. Registering a knowledge source, triaging ingestion failures,
and searching audit logs are dense, multi-column, consequence-bearing tasks. Cramming them
into 375 px produces an interface where a mis-tap changes a classification. Admin is
therefore **read-only on small screens** — dashboards and run status render, mutations
require a larger viewport. That is a considered restriction, not a missing feature.

Three implementation consequences: the citation drawer becomes a bottom sheet under
768 px; the message list virtualizes earlier because mobile memory is tighter; and streamed
tokens batch at a longer interval on low-end devices, since re-rendering markdown at 30 ms
frames is comfortable on a laptop and not on a mid-range phone.

## 7. Accessibility & internationalization

- WCAG 2.2 AA target. Full keyboard path: composer → send → citation chips → drawer → open.
- Streaming answers announce via `aria-live="polite"` with the *completed* answer, not
  per token — per-token announcements make a screen reader unusable.
- `AnswerStateBanner` uses `role="status"`; conflict uses `role="alert"`.
- Colour is never the sole carrier of answer state (icon + text always accompany it).
- Language affects more than the interface: see `03-database-schema.md` §3.5 for how
  document language drives keyword stemming and why the embedding model must be
  multilingual. A translated *quote* is never shown — it would no longer be verifiable
  against the authoritative source (BR-004).
- The SRS flags i18n as an open item (§6.7). The UI is built i18n-ready anyway:
  `next-intl` message catalogs, no hardcoded strings, logical CSS properties
  (`margin-inline-start`) throughout, and `lang`/`dir` driven by user preference.
  AUC operates in Arabic, English, French, Portuguese, Kiswahili and Spanish — RTL
  support retrofitted later is expensive; built in from the start it is nearly free.

## 8. Admin console

Different design goals: information density over polish, and every destructive or
governance-significant action confirmed and attributed.

- **Overview** (FR-048): health tiles (API/DB/vector/LLM), indexed document count,
  active sources, failed ingestions needing attention, 24 h query volume, feedback
  ratio, refusal rate.
- **Ingestion detail** (FR-050): per-document error codes with the failing stage and
  retry action, not an opaque "failed".
- **Documents** (FR-046): saved filters for the lifecycle questions admins actually
  ask — *expired but still indexed*, *review required*, *never synced*, *high
  injection risk*.
- **Audit** (FR-051): correlation-ID search that reconstructs a full request timeline
  across auth → query → retrieval → generation, which is what an investigation needs.
- **Quality** (FR-054): groundedness and citation-accuracy trend against the §6.8
  thresholds, with the current eval-run verdict.

Every mutation shows *who* and *when* inline. In a governance-heavy institution,
attribution visible in the UI is what makes the audit log usable rather than merely present.
