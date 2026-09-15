"""Evaluation harness (SRS §7.5).

Quality targets that live only in a document are aspirations. The same targets
wired into something that can fail a build are requirements.

Each question is executed **as a specific identity**, which is what makes the
security assertions meaningful: `must_not_retrieve` checks that a named user
does *not* see a named document, and that cannot be expressed without running
the real pipeline under a real authorization context.

Deliberately no LLM judge here. Groundedness and citation accuracy are scored
by the pipeline itself and read back from the message; what this harness adds is
the deterministic half — did retrieval find the right family, did the answer land
in the expected state, did anything unauthorized appear. Those are the checks
that gate a build. Semantic judging needs a second model and a human calibration
set (see the evaluation-harness note in the architecture documentation) and is Phase 2 work.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from askau.domain.authz import AuthorizationContext, PrincipalId, UserId
from askau.domain.enums import AnswerState
from askau.rag.orchestrator import RagOrchestrator


@dataclass(frozen=True, slots=True)
class EvalQuestion:
    id: str
    question: str
    as_username: str | None = None
    expected_state: str | None = None
    expected_families: tuple[str, ...] = ()
    must_not_retrieve: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    #: A failure that is understood and cannot be fixed at this layer. Reported,
    #: excluded from the gate. Kept rather than deleted so that when the cause is
    #: removed the question starts passing and someone notices — a deleted test
    #: never tells you it was fixed.
    known_limitation: str | None = None


@dataclass(slots=True)
class QuestionResult:
    question_id: str
    question: str
    passed: bool
    actual_state: str
    retrieved_families: tuple[str, ...] = ()
    #: Carried onto the result so precision can be computed without re-reading
    #: the question set.
    expected_families: tuple[str, ...] = ()
    failure: str | None = None
    latency_ms: int = 0
    groundedness: float | None = None
    #: Sentences the lexical measure could not match to the evidence. Carried so
    #: a low score can be read rather than guessed at: an answer that drifted
    #: from its sources and one that reasoned about which source it preferred
    #: both land below 1.0, and only these sentences separate them.
    unsupported: tuple[str, ...] = ()
    known_limitation: str | None = None


def _as_sentences(raw: object) -> tuple[str, ...]:
    """Narrow the untyped diagnostics bag, the way `_topics_of` does elsewhere."""
    if isinstance(raw, tuple | list):
        return tuple(str(x) for x in raw)
    return ()


def _precision(results: list[QuestionResult]) -> float | None:
    """Retrieved-and-wanted over retrieved, across questions that declare both.

    Read this alongside `retrieval_top_k`, not on its own. Retrieval returns a
    fixed number of chunks, so a question expecting one document out of eight
    returned is capped at 12% however perfectly they are ranked. This number
    therefore mostly measures how much context is handed to the model, and
    barely moves when ranking improves — which is exactly what was observed
    switching from hash embeddings to bge-m3 (18% → 19%).

    Kept because "how much of what we send is relevant" is still worth knowing —
    it is the model's signal-to-noise ratio. But `mrr` below is the number that
    answers whether retrieval is any good.
    """
    scored = [r for r in results if r.expected_families and r.retrieved_families]
    if not scored:
        return None
    per_question = [
        len(set(r.retrieved_families) & set(r.expected_families)) / len(set(r.retrieved_families))
        for r in scored
    ]
    return sum(per_question) / len(per_question)


def _mrr(results: list[QuestionResult]) -> float | None:
    """Mean reciprocal rank of the first wanted document.

    The ranking metric. 1.0 means the right document was always first; 0.5 means
    typically second; 0.0 means never retrieved at all. Unlike precision it is
    independent of how many chunks are returned, so it isolates the thing the
    semantic arm is actually responsible for.
    """
    scored = [r for r in results if r.expected_families and r.retrieved_families]
    if not scored:
        return None

    def reciprocal(r: QuestionResult) -> float:
        wanted = set(r.expected_families)
        for position, title in enumerate(r.retrieved_families, start=1):
            if title in wanted:
                return 1.0 / position
        return 0.0

    return sum(reciprocal(r) for r in scored) / len(scored)


@dataclass(slots=True)
class EvalReport:
    results: list[QuestionResult] = field(default_factory=list)
    started_at: float = 0.0
    duration_ms: int = 0

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failures(self) -> list[QuestionResult]:
        """Failures that count. Known limitations are reported separately."""
        return [r for r in self.results if not r.passed and not r.known_limitation]

    @property
    def known(self) -> list[QuestionResult]:
        return [r for r in self.results if not r.passed and r.known_limitation]

    def metrics(self) -> dict[str, Any]:
        answered = [r for r in self.results if r.groundedness is not None]
        recall_scored = [
            r for r in self.results if r.question_id.startswith("ret-") or r.retrieved_families
        ]
        return {
            "questions": self.total,
            "passed": self.passed,
            "known_limitations": len(self.known),
            # Known limitations are excluded from the denominator: a gate that
            # counts an understood, unfixable-at-this-layer failure as a
            # regression is a gate people learn to override.
            "pass_rate": (
                self.passed / (self.total - len(self.known))
                if self.total - len(self.known)
                else None
            ),
            "groundedness": (
                sum(r.groundedness or 0 for r in answered) / len(answered) if answered else None
            ),
            # The worst single answer, not the average.
            #
            # An average hides the case that matters: nineteen good answers and
            # one that wandered off its sources average to 95%, and 95% passes.
            # The measure is a drift detector, and drift happens to individual
            # answers, so the gate reads the worst one.
            "min_groundedness": (min(r.groundedness or 0 for r in answered) if answered else None),
            "p95_latency_ms": (
                sorted(r.latency_ms for r in self.results)[int(len(self.results) * 0.95) - 1]
                if self.results
                else None
            ),
            "retrieval_checked": len(recall_scored),
            # Of the documents retrieved for questions that declare what they
            # expect, what fraction were actually wanted.
            #
            # Recall — "did we find the right document" — is what `pass_rate`
            # already covers, and the keyword arm alone satisfies it for most
            # questions. Precision is what the *semantic* arm is for, and it is
            # the number that collapses when embeddings carry no meaning: a hash
            # embedder fills the remaining slots with whatever happens to hash
            # nearby, so the right answer arrives surrounded by noise. A reader
            # sees that noise as "sources", which is worse than a shorter answer.
            "retrieval_precision": _precision(self.results),
            "mrr": _mrr(self.results),
        }

    def gate(self, thresholds: dict[str, float]) -> tuple[bool, list[str]]:
        """Apply the §6.8 acceptance thresholds.

        Security failures are absolute: any one of them fails the gate whatever
        the aggregate says. A build that leaks one document to one user is not
        95% acceptable.
        """
        breaches: list[str] = []
        security_failures = [r for r in self.failures if "unauthorized" in (r.failure or "")]
        if security_failures:
            breaches.append(
                f"{len(security_failures)} unauthorized-retrieval failure(s) — "
                "the acceptance criterion for this is zero"
            )

        m = self.metrics()
        if m["pass_rate"] is not None and m["pass_rate"] < thresholds.get("pass_rate", 0.9):
            breaches.append(f"pass rate {m['pass_rate']:.0%} below {thresholds['pass_rate']:.0%}")
        # Gated on the worst answer, not the mean.
        #
        # The mean was thresholded at 90% for as long as the answer generator was
        # a stub that returned retrieved text verbatim and therefore scored 1.00
        # on every question by construction. That number was never measured
        # against a model that paraphrases; the first real one produced 86% and
        # failed a gate it had no way of passing.
        #
        # What the lexical measure is actually for — its own module says so — is
        # catching an answer that has drifted away from its sources. It does not
        # claim to catch subtle misstatement, and it penalises a model for
        # explaining which source it preferred, because that sentence's words are
        # not in the evidence. So it is gated as a floor: no single answer may
        # fall below `min_groundedness`, and the mean is reported but not gated.
        #
        # A real quality bar needs the model-based judge that `rag/grounding.py`
        # refers to and which does not exist. Until it does, this gate catches
        # drift and makes no claim about quality.
        floor = thresholds.get("min_groundedness")
        worst = m["min_groundedness"]
        if floor is not None and worst is not None and worst < floor:
            breaches.append(
                f"an answer scored {worst:.0%} groundedness, below the {floor:.0%} drift floor"
            )
        return (not breaches, breaches)


_AUTHZ_SQL = text("""
    SELECT u.id::text AS uid, u.acl_version, u.department, u.email,
           ARRAY(SELECT principal_id FROM user_principals WHERE user_id = u.id) AS principals
    FROM users u WHERE u.entra_oid = :oid
""")


class EvaluationRunner:
    def __init__(self, orchestrator: RagOrchestrator, engine: AsyncEngine) -> None:
        self._orch = orchestrator
        self._engine = engine
        self._contexts: dict[str, AuthorizationContext] = {}

    async def run(self, questions: list[EvalQuestion]) -> EvalReport:
        report = EvalReport(started_at=time.perf_counter())
        for q in questions:
            report.results.append(await self._one(q))
        report.duration_ms = int((time.perf_counter() - report.started_at) * 1000)
        return report

    async def _authz(self, username: str) -> AuthorizationContext:
        if username not in self._contexts:
            async with self._engine.connect() as conn:
                row = (await conn.execute(_AUTHZ_SQL, {"oid": f"oid-{username}"})).mappings().one()
            self._contexts[username] = AuthorizationContext(
                user_id=UserId(row["uid"]),
                principals=frozenset(PrincipalId(p) for p in row["principals"]),
                acl_version=row["acl_version"],
                department=row["department"],
                email=row["email"],
            )
        return self._contexts[username]

    async def _one(self, q: EvalQuestion) -> QuestionResult:
        authz = await self._authz(q.as_username or "staff.misd")
        started = time.perf_counter()
        answer, _ = await self._orch.answer(q.question, authz)
        latency = int((time.perf_counter() - started) * 1000)

        # In *rank order*, de-duplicated, not sorted alphabetically. Sorting
        # discards the one thing that distinguishes good ranking from bad: a set
        # containing the right document says nothing about whether it was first
        # or eighth.
        seen: dict[str, None] = {}
        for c in answer.retrieval.chunks if answer.retrieval else ():
            seen.setdefault(c.document_title, None)
        families = tuple(seen)
        result = QuestionResult(
            question_id=q.id,
            question=q.question,
            passed=True,
            actual_state=answer.state.value,
            retrieved_families=families,
            expected_families=q.expected_families,
            latency_ms=latency,
            groundedness=answer.groundedness,
            unsupported=_as_sentences(answer.diagnostics.get("unsupported_sentences")),
            known_limitation=q.known_limitation,
        )

        # Security first: an unauthorized document in the result set fails the
        # question regardless of how good the answer was.
        leaked = [t for t in q.must_not_retrieve if t in families]
        if leaked:
            result.passed = False
            result.failure = f"unauthorized retrieval: {', '.join(leaked)}"
            return result

        if q.expected_state and answer.state is not AnswerState(q.expected_state):
            result.passed = False
            result.failure = f"expected state {q.expected_state}, got {answer.state.value}"
            return result

        missing = [t for t in q.expected_families if t not in families]
        if missing:
            result.passed = False
            result.failure = f"did not retrieve: {', '.join(missing)}"
        return result
