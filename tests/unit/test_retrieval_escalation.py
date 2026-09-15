"""Higher intelligence: named profiles and the escalating second pass (FR-016).

The client's toggle reads *"AskAU can automatically use more thorough retrieval
when answering complex questions."* Two claims are packed into that sentence and
both are tested here:

* **automatically** — the reader consents, the system decides. A caller cannot
  ask for a thorough pass; it happens when the evidence gate is unconvinced and
  not otherwise.
* **more thorough retrieval** — the second pass genuinely asks for more, and the
  amount is chosen server-side from the configured baseline rather than sent by
  a client.

Driven by a fake retriever rather than the database. What is being asserted is
the orchestration decision — when a second pass happens, what it asks for, and
whether its result is adopted — and a real corpus would make those assertions
depend on how a particular question happens to rank today.
"""

from __future__ import annotations

from askau.domain.answer import AnswerState
from askau.domain.authz import AuthorizationContext, PrincipalId, UserId
from askau.domain.enums import Classification, RetrievalStrategy
from askau.domain.profiles import RetrievalTier, profile_for
from askau.domain.retrieval import (
    ChunkId,
    DocumentId,
    RetrievalQuery,
    RetrievalResult,
    RetrievedChunk,
)
from askau.llm.ports import CompletionChunk
from askau.rag.orchestrator import RagOrchestrator
from askau.settings import Settings


class TestProfiles:
    def test_standard_is_exactly_the_configured_baseline(self) -> None:
        """Otherwise introducing tiers would silently change every existing
        deployment's behaviour, which is not what adding a toggle should do."""
        p = profile_for(RetrievalTier.STANDARD, candidate_k=60, top_k=8, rerank_input_k=40)
        assert (p.candidate_k, p.top_k, p.rerank_input_k) == (60, 8, 40)

    def test_thorough_widens_candidates_but_not_the_prompt(self) -> None:
        """More candidates give the reranker more to choose from — the point.

        More chunks in the prompt is a different change and a worse one: it
        dilutes the context, costs tokens linearly, and pushes the material that
        matters further from the instruction. So `top_k` must not move.
        """
        p = profile_for(RetrievalTier.THOROUGH, candidate_k=60, top_k=8, rerank_input_k=40)
        assert p.candidate_k > 60
        assert p.rerank_input_k > 40
        assert p.top_k == 8

    def test_thorough_scales_from_configuration_rather_than_fixed_numbers(self) -> None:
        """An operator who tunes the baseline down for a small corpus should not
        find the escalated path still reaching for the default's multiple."""
        small = profile_for(RetrievalTier.THOROUGH, candidate_k=10, top_k=4, rerank_input_k=8)
        assert small.candidate_k == 40

    def test_thorough_cannot_exceed_the_ceiling_settings_enforces(self) -> None:
        """Escalation must never ask for more work than an operator could have
        configured deliberately — `Settings` caps `candidate_k` at 500."""
        p = profile_for(RetrievalTier.THOROUGH, candidate_k=400, top_k=8, rerank_input_k=150)
        assert p.candidate_k == 500
        assert p.rerank_input_k == 200


def _ctx() -> AuthorizationContext:
    return AuthorizationContext(
        user_id=UserId("u-1"), principals=frozenset({PrincipalId(1)}), acl_version=1
    )


def _chunk(i: int, text: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=ChunkId(i),
        document_id=DocumentId(f"doc-{i}"),
        content=text,
        score=0.9,
        document_title="Staff Leave Policy",
        source_uri="https://example.invalid/leave",
        source_name="Policy Repository",
        classification=Classification.INTERNAL,
    )


class RecordingRetriever:
    """Returns nothing on the first pass and real content on the second.

    That is the shape escalation exists for: a question whose material the
    narrow pass missed. Recording every `candidate_k` it was asked for is what
    makes "did it actually widen" checkable rather than inferred.
    """

    def __init__(self, *, second_pass_chunks: int) -> None:
        self.candidate_ks: list[int] = []
        self._second = second_pass_chunks

    async def search(self, query: RetrievalQuery) -> RetrievalResult:
        self.candidate_ks.append(query.candidate_k)
        first = len(self.candidate_ks) == 1
        chunks = (
            ()
            if first
            else tuple(
                _chunk(i, "Staff accrue thirty days of annual leave each calendar year.")
                for i in range(self._second)
            )
        )
        return RetrievalResult(
            chunks=chunks,
            strategy=RetrievalStrategy.HYBRID,
            candidates_considered=query.candidate_k,
            took_ms=1,
        )


_ANSWER = "Staff accrue thirty days of annual leave each calendar year. [1]"


class StubEmbedder:
    model_name = "stub"
    dimensions = 8

    async def embed_query(self, text: str) -> list[float]:
        return [0.1] * 8

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 8 for _ in texts]


class StubLLM:
    """Echoes a grounded sentence with a citation marker.

    Deliberately not a mock that records calls: what these tests assert is the
    retrieval decision, and a stub that always answers the same way keeps the
    generation step from being a variable in results about escalation.
    """

    provider = "stub"
    model_name = "stub"

    async def complete(self, system: str, user: str, *, max_tokens: int = 800) -> str:
        return _ANSWER

    async def stream(self, system: str, user: str, *, max_tokens: int = 800):  # type: ignore[no-untyped-def]
        yield CompletionChunk(text=_ANSWER)


def _orchestrator(retriever: RecordingRetriever) -> RagOrchestrator:
    return RagOrchestrator(
        retriever=retriever,  # type: ignore[arg-type]
        embedder=StubEmbedder(),  # type: ignore[arg-type]
        llm=StubLLM(),  # type: ignore[arg-type]
        settings=Settings(),
    )


_QUESTION = "How many days of annual leave do staff accrue each year?"


class TestEscalationIsConsentedNotRequested:
    async def test_without_consent_a_weak_result_is_refused_and_nothing_widens(self) -> None:
        r = RecordingRetriever(second_pass_chunks=3)
        answer, trace = await _orchestrator(r).answer(_QUESTION, _ctx())

        assert answer.state is AnswerState.INSUFFICIENT_EVIDENCE
        assert trace.escalated is False
        # One pass, at the configured width. The important half of this
        # assertion is the length: a second pass that happened without consent
        # would be spending on behalf of someone who declined.
        assert r.candidate_ks == [Settings().retrieval_candidate_k]

    async def test_with_consent_a_weak_result_triggers_one_wider_pass(self) -> None:
        r = RecordingRetriever(second_pass_chunks=3)
        answer, trace = await _orchestrator(r).answer(_QUESTION, _ctx(), allow_escalation=True)

        assert trace.escalated is True
        assert len(r.candidate_ks) == 2, "escalation must be one further pass, not a loop"
        assert r.candidate_ks[1] > r.candidate_ks[0]
        # The pass that found the material is the one that answers. Widening
        # that could not change the outcome would be pure cost.
        assert answer.state is AnswerState.GROUNDED

    async def test_a_sufficient_first_pass_never_escalates_even_with_consent(self) -> None:
        """The answers that were already grounded must not pay for the feature.

        This is the assertion that keeps "higher intelligence" from quietly
        becoming "every question costs twice as much".
        """

        class AlwaysGood(RecordingRetriever):
            async def search(self, query: RetrievalQuery) -> RetrievalResult:
                self.candidate_ks.append(query.candidate_k)
                return RetrievalResult(
                    chunks=tuple(
                        _chunk(i, "Staff accrue thirty days of annual leave each calendar year.")
                        for i in range(4)
                    ),
                    strategy=RetrievalStrategy.HYBRID,
                    candidates_considered=query.candidate_k,
                    took_ms=1,
                )

        r = AlwaysGood(second_pass_chunks=0)
        _, trace = await _orchestrator(r).answer(_QUESTION, _ctx(), allow_escalation=True)
        assert trace.escalated is False
        assert len(r.candidate_ks) == 1


class TestEscalationDoesNotLowerTheBar:
    async def test_a_wider_pass_that_still_finds_nothing_is_still_a_refusal(self) -> None:
        """Escalation buys another look, never a lower threshold.

        Getting this wrong is how a "more thorough" setting turns into a setting
        that fabricates answers for the people who switched it on.
        """
        r = RecordingRetriever(second_pass_chunks=0)
        answer, trace = await _orchestrator(r).answer(_QUESTION, _ctx(), allow_escalation=True)
        assert trace.escalated is True
        assert answer.state is AnswerState.INSUFFICIENT_EVIDENCE
        assert answer.diagnostics.get("escalated") is True


class TestBothPathsEscalateAlike:
    async def test_streaming_reaches_the_same_verdict_as_buffered(self) -> None:
        """The buffered and streaming paths share one gate on purpose.

        They have drifted before — there were once two SSE helpers serializing
        differently — and a gate that escalated on one path only would answer or
        refuse the same question depending on which endpoint was called.
        """
        buffered, _ = await _orchestrator(RecordingRetriever(second_pass_chunks=3)).answer(
            _QUESTION, _ctx(), allow_escalation=True
        )

        r = RecordingRetriever(second_pass_chunks=3)
        final = None
        async for item in _orchestrator(r).stream(_QUESTION, _ctx(), allow_escalation=True):
            if hasattr(item, "state"):
                final = item
        assert final is not None
        assert final.state is buffered.state
        assert len(r.candidate_ks) == 2
