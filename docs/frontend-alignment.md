# Backend ↔ frontend alignment: audited, and tested through the UI

**Date:** 2026-09-02 · **Backend:** this repo · **Client:** `askau-frontend`

The short answer to "do they align": **they do now.** They did not before this audit, and
the gaps were not the ones a field-by-field comparison suggests.

---

## The finding that mattered

Their `features/chat/components/ai-message.tsx` is a chain of `message.state === "x" &&`
blocks **with no default branch**. `AnswerOut` — the live ask response — had no `state`
field at all, and no `sources` in the shape those components render.

So a live answer arrived with nothing to match and rendered an **empty message**, while the
*same turn* re-read from the transcript rendered correctly, because `stored_message_to_wire`
did supply both. One answer, two endpoints, two different shapes.

Fixed on our side. `AnswerOut`, `DoneEvent` and `SourcesEvent` now carry `state`,
`sources`, `groundingCount` and `conflictingSources`, derived exactly as `MessageOut`
derives them — including the promotion to `outdated` when an answer rests on superseded
material, which had also been stored-path only.

---

## Their two type files disagree with each other

This is what made the audit non-obvious, and most of it is **not ours to fix**.

| Field | `types/*.ts` (their declared API contract) | `features/chat/types/index.ts` (what the UI renders) | We send |
|---|---|---|---|
| Message time | `createdAt: ISODateTime` | `timestamp: Date` | `createdAt` — matches the contract |
| Message state | `status: pending\|streaming\|complete\|error` | `state?: MessageState` | both `state` and `answerState` |
| Feedback rating | `"helpful" \| "not_helpful"` | `"helpful" \| "not-helpful"` | `not_helpful` — matches the contract |
| Conversation preview | `lastMessagePreview?` | `preview` | `lastMessagePreview` — matches the contract |
| Conversation time | `createdAt` / `updatedAt` | `timestamp: Date` | both — matches the contract |

We match `types/*.ts` on every field. The UI-facing type differs from their own API
contract on naming and on hyphen-vs-underscore, and converting between them is ordinary
frontend adaptation — JSON cannot carry a `Date` regardless. **Not a backend problem, but
worth their team knowing the two files have drifted.**

---

## For their team

**1. `Source` has no `hasAccess`.** We send it. FR-031 separates grounding from opening: a
source may support an answer the reader is not allowed to open. Without the field the UI
cannot mark that, so an unopenable source looks identical to an openable one until the
click fails.

**2. `NotHelpfulReason` has five values; ours has seven, and they do not correspond.**
Their API contract (`FeedbackRequest`) declares no reason field at all, so this was
undefined between us. The mapping now in `features/chat/lib/backend.ts` is:

| UI | backend |
|---|---|
| `incorrect` | `incorrect_answer` |
| `irrelevant` | `not_relevant` |
| `missing-source` | `missing_information` |
| `outdated` | `outdated_information` |
| `other` | `other` |

`wrong_source` and `unclear` are unreachable from the UI. Either add them or drop them.

**3. Negative feedback needs its reason in the same call.** The backend rejects
`not_helpful` without one — deliberately, since a negative rating nobody can act on is
just a number (FR-044). Their UI collects the thumb first and the reason second, so the
thumb alone was a guaranteed 422. The client now defers the write until a reason is
chosen. Worth confirming that is the intended interaction rather than a backend
constraint to relax.

**4. The `insufficient` branch discards our explanation.** Confirmed live. That branch
renders fixed translated copy and never `message.content`, so our text — *"this may mean
the relevant document has not been added yet, that it sits outside your access, or that
the question needs rephrasing"* — is thrown away. FR-028 requires AskAU to state that it
cannot answer; for `clarification_needed`, `out_of_scope` and `refused_safety` (all mapped
to `insufficient`) that statement currently cannot reach the reader.

---

## What was added to `askau-frontend` to make UI testing possible

The client made **no live backend calls** — `apiClient` was imported by nothing and
`chat-shell.tsx` faked answers with `setTimeout`. Testing through the UI was impossible
without wiring it. These are additive and dev-scoped:

| File | What |
|---|---|
| `app/api/[...path]/route.ts` | The server-side proxy the plan assigned to their team. Attaches the bearer server-side and streams responses through, so SSE stays incremental. |
| `features/chat/lib/backend.ts` | Typed calls for ask, feedback and conversation listing. |
| `features/chat/components/chat-shell.tsx` | `handleSend`, `handleFeedback`, `handleReason` now call the backend. |
| `features/auth/components/dev-bypass-button.tsx` | An identity picker over the seeded users. |
| `lib/auth/config.ts` | The mock provider honours the submitted username instead of returning one fixed user. |
| `.dev-tokens.json` | Generated, gitignored. |

**Why the identity picker matters.** The mock provider returned a single fixed user, which
makes the entire authorization layer look like a no-op — every reader has the same access
list. The product's central claim is that two people asking the same question get
different answers, and that is not observable from one identity.

---

## Tested through the UI

Backend at `:8080`, client at `:3000`, Entra simulated by the credentials provider,
SharePoint simulated by the seeded filesystem corpus.

| Feature | Result |
|---|---|
| Grounded answer | Real answer, "Grounded in 3 approved sources", three cards with section, page, version and classification pill |
| Insufficient evidence | *"What is the capital of Brazil?"* → refusal panel with next steps |
| **Permission-aware retrieval** | *"procedure for reallocating budget between programmes"* → `staff.finance` gets a grounded answer citing the **confidential** *Budget Reallocation Procedure*; `staff.hr` gets a refusal with **zero** sources |
| Conflict detection | *"daily subsistence allowance for continental travel"* → amber "Conflicting information found", answer showing both USD 180 and USD 150 across approved documents |
| Feedback | Thumbs-down → "Missing source" → stored as `missing_information`, attributed to `staff.hr` |

Backend: 607 tests passing, 3/3 import contracts, contract regenerated.

### Round two — the rest of it

The first pass left three features "not tested via UI", which was a poor way to put it: the
backend was implemented and tested for all three, and it was the *frontend* components that
were still on fixtures. Wired and tested:

| Feature | Result |
|---|---|
| Preferences load from the backend | `shareAnalytics` shows **on**, where the fixture defaulted it off — proof the modal reads rather than guesses |
| Partial PATCH | Toggling `saveHistory` off persisted, and `shareAnalytics` stayed `true` — one toggle does not rewrite the others |
| Higher intelligence | Shows **off**, matching the backend default where the fixture had it on. Toggling it persisted `higher_intelligence=true` and left the other two alone |
| **`saveHistory` off, end to end** | Grounded answer with three source cards delivered; conversations **1 → 1**, messages **2 → 2**, query audit **2387 → 2388**, and no question text in `detail` (ADR-0018) |
| Delete all history | `staff.hr` **1 → 0**; the other **507** conversations belonging to other accounts untouched; audited as `conversation.deleted` with actor and count |
| Conversation history | Sidebar shows the reader's own conversations; the six invented titles are gone |
| Transcript replay | A stored turn renders identically to the live one — same state pill, same three cards. The conflict conversation replays with its banner intact |
| Knowledge-base registry | "AUC Policy Library (synthetic) — **13 document(s) you can read** · MISD · Not yet synced", with an Active pill. The fixture claimed "1,842 documents · v2.6" |

Three fixes were needed along the way, all worth recording because each looked like
something else:

* **Dev tokens expired mid-session.** `DevTokenVerifier.issue` defaults to one hour, which
  is right for a stand-in credential and wrong for a file whose purpose is letting somebody
  drive the interface. It surfaced as a 401 on `DELETE` that read like a method problem.
  Re-minted at 30 days.
* **The proxy cached the token file in a module variable.** After re-minting it kept
  serving the expired ones. The cache saved one small synchronous read and cost a debugging
  detour; removed.
* **`window.confirm` on delete-all-history.** My own first version. It is unstyled, blocking,
  ignores the app's design system, and its buttons are in the *browser's* language rather
  than the one the reader chose — which for a four-locale product is a bug on its own. It is
  also invisible to automation, so the single most destructive control in the product was the
  one that could not be tested. Replaced with an inline two-step confirmation, translated
  into all four locales.

### Round three — streaming, follow-ups, provenance

| Feature | Result |
|---|---|
| **SSE streaming** | Real stage events replace the `setInterval` that invented progress. Three stages in order, sources frame before the first token, tokens appended as they arrive, `done` swapping in the backend's message id so feedback works on a streamed answer |
| Proxy does not buffer | Measured through the Next route: **13 separate chunks over 53 ms**, `text/event-stream` preserved. A buffering proxy would have delivered one chunk at the end and the streaming would have been decorative |
| **Follow-up questions (FR-004)** | *"Does that apply to staff on probation?"* — standalone: `insufficient`, no sources. After a leave question: `grounded`, citing *Annual Leave Policy*. Verified on both the buffered and streaming paths |
| Source preview (FR-029) | The pane shows real backend metadata — department, section, page, version, effective date, status — plus the quoted extract. A citation the reader can actually check |
| **Source download (BR-004)** | Was `onDownload={() => {}}` in two places. Now opens the backend-vended `accessUrl`, which redirects to the authoritative original rather than serving it — and audits the access: `document.opened` recorded with actor and document id |
| Unopenable sources | Grounding and opening are separate permissions. The button is disabled with an explanation when the backend omits `accessUrl`, rather than looking available and failing on click |

**A bug found by wiring the stream.** The pre-generation `sources` frame was arriving
**empty**. `_citations_of` has a fallback to the retrieved set for exactly that frame — the
citations do not exist yet, which is the frame's whole purpose — and `_sources_of`, which I
had added alongside it, did not. The panel would have stayed blank until `done`, which is
the same as not having the frame. It now carries the 8 retrieved sources and narrows to the
3 actually cited when the answer completes.

**Also extended their `Source` type** with `hasAccess` and `accessUrl`. We were already
sending both; the type omitting them was gap 1 in the notes above, and the download button
could not be wired without them.

**Still fixture-backed and not wired:** the Notifications section (deferred — no
subscription or mail infrastructure exists), and the Security section (recommended for
removal; its "1 active session · Last seen: now" is hardcoded, confirmed on screen).

**Still untested through the UI:** the four locales including Arabic RTL, the `outdated`
banner (needs a superseded document in the live corpus), `shareAnalytics` as a sampling gate
(API-level only), rate limiting and token expiry, and every admin operation — there is no
admin UI in Phase 1, so those 26 mutating endpoints are covered at API level only.
