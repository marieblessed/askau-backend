# Settings screen: what the backend now supports, and what it cannot

**To:** the `askau-frontend` team · **From:** the API team · **Date:** 2026-09-02

Your settings modal has six sections. The controls are built, translated into four
locales, and wired to local `useState` — nothing reaches a server. This is the reply
you asked for: one verdict per control, and what changed on our side.

A shipped toggle that silently does nothing is worse than an absent one. Somebody who
switches off *Save conversation history* has been told their questions are not kept,
and until this week `messages` recorded every one. That is the standard we applied to
each control below.

---

## Summary

| Control | Verdict | Endpoint |
|---|---|---|
| Save conversation history | **Built** | `GET`/`PATCH /api/v1/users/me/preferences` |
| Delete all history | **Built** | `DELETE /api/v1/users/me/history` |
| Higher intelligence | **Built** | same preferences endpoint |
| Share usage analytics | **Built**, with a copy change | same preferences endpoint |
| Knowledge bases | **Built**, read-only | `GET /api/v1/knowledge-bases` |
| Notifications | **Defer** | — |
| MFA enrolment | **Remove** — Entra's | — |
| Active sessions / sign out others | **Remove** — currently fiction | — |
| Session timeout | **Remove** — yours, not ours | — |
| Composer attach button | **Remove** — contradicts the product | — |

---

## Built

### Save conversation history · Delete all history

`GET /api/v1/users/me/preferences` → `{ saveHistory, shareAnalytics, higherIntelligence }`.
`PATCH` the same path with any subset; omitted fields keep their value, so changing one
toggle never resets another.

Three things worth knowing before you wire it.

**With history off, nothing is written at all.** `POST /conversations` returns a uuid that
exists only for the request. You still get a real answer with real sources; there is simply
no row afterwards, and nothing appears in the conversation list.

**`messageId` comes back `null` for those turns.** Feedback has a foreign key to messages,
so there is nothing to rate. Your `AnswerActions` already renders the feedback bar only
when a `messageId` is present, so this needs no change on your side — but it is deliberate,
not a bug.

**The audit record still exists.** That someone asked something, and which documents their
question reached, is a security record the AUC requires and a user cannot decline. It never
contains the question or the answer text. If anyone asks, that distinction is the whole
design, and it is asserted by a test.

`DELETE /api/v1/users/me/history` → `{ deleted: n }`. Scoped to the caller. Rows are gone,
not soft-deleted, and the deletion itself is audited.

### Higher intelligence

Same endpoint, `higherIntelligence`. **Defaults off**, unlike the other two — those are
opt-outs of useful behaviour, this one authorises extra work per question.

Your copy is what we built to: *"AskAU can automatically use more thorough retrieval when
answering complex questions."* So it is consent, not a quality dial. When the evidence gate
is unconvinced and this is on, we retrieve again with a wider net and re-assess. When the
first pass was already good, nothing extra happens and nothing extra is spent.

**The client cannot set retrieval width.** A request body carrying `topK`, `candidateK` or
`tier` is now a **422** rather than silently ignored. Retrieval width is how much work the
database is asked to do, and a browser that can set it can ask for anything.

### Share usage analytics — **please change this copy**

Current copy: *"Help improve AskAU by sharing anonymised usage data."* That describes
telemetry, and telemetry is not what this can control — audit logging and resource
reporting are both required and cannot be declined. A toggle implying otherwise is a
promise the product cannot keep.

What it actually gates: **may this person's questions be sampled for quality measurement.**
Only questions that went badly are sampled — a refusal, a conflict, a thin answer. Nothing
identifying is stored: not the user, not their access footprint, not the answer.

Suggested copy: *"Allow my questions to be used to improve AskAU's answers. Only questions
AskAU struggled with are kept, without anything identifying you."*

### Knowledge bases

`GET /api/v1/knowledge-bases` → `ListResponse` of
`{ id, name, department, sourceType, status, documentCount, lastSyncedAt, lastSyncStatus }`.

**Read-only, and it should stay that way.** Switching a source on is an approval act that
belongs to a knowledge administrator, not to a reader's preferences. Your status pills are
the right presentation. The write verbs return 405.

Two mismatches to settle rather than paper over:

**There is no version.** Your UI renders `"v2.6"`; `knowledge_sources` has no version
column, because a revision concept was never built. We are not inventing a number that
would be displayed as provenance. Use `lastSyncedAt`, or tell us a revision concept is
needed and we will design one.

**`documentCount` is the *caller's* count, not the repository's.** Two people will see
different numbers for the same knowledge base, which is correct — the size of a repository
someone cannot read is information about it. A source they can reach nothing in is absent
from the list rather than shown as empty.

Related: your grounding pill stamps one knowledge base per answer (`"Policy Repo v2.6"`),
but retrieval spans sources within a single query. We return the set an answer drew on;
the pill needs to render either the single name or "N sources". Restricting each answer to
one source would make answers worse.

---

## Defer

**Notifications** — email summaries, *"Notify me when approved documents are updated"*,
session-expiry alerts. This is genuinely new infrastructure: document-change subscriptions
plus an outbound mail pipeline, neither of which exists anywhere in the platform, and
neither in the SRS scope we traced. Not a small addition to an existing seam.

---

## Remove

### The whole Security section

This is the section most likely to be read as a security assurance, and at the moment it is
the least accurate.

| Control | Why not us |
|---|---|
| *"Set up MFA"* / *"MFA is not currently enabled"* | Enrolment is an Entra Conditional Access policy. The backend cannot enrol anyone and should not appear to. If the AUC wants MFA it is configured in the tenant; deep-link to Entra's own security page |
| *"1 active session · Last seen: now"* | **Currently hardcoded.** Sessions are NextAuth JWTs in a cookie with an 8-hour `maxAge`; nothing server-side tracks them, so nothing can enumerate or revoke them. Real global sign-out is an Entra token-revocation call |
| Session timeout (15m / 30m / 1h / 4h) | Session lifetime is set in your `lib/auth/config.ts`. A per-user override has to be enforced by whoever mints the session, which is NextAuth, not us |

Our recommendation: make it a read-only status panel sourced from Entra, or drop it for
Phase 1. Displaying *"Last seen: now"* as though it were measured is the specific thing to
fix first, whatever else you decide.

### The composer's attach button

It contradicts the product's premise that answers come only from approved sources. A user
who can attach a document and be answered from it has bypassed the approval rule by design.
The button currently has no handler, so removing it costs nothing.

---

## Nothing to do

**`AgentAction`.** Your types mark it `@future Phase 2+`, and our `tool_registry` and
`approval_requests` seams already anticipate this shape (`requiresApproval`,
`awaiting_approval`). The alignment is already correct.
