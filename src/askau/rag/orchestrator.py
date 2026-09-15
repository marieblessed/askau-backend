"""The RAG query path — one entry point, twelve stages.

Reading this file top to bottom is reading the whole pipeline, which is
deliberate: a security reviewer has to be able to see that authorization
precedes generation without following calls through a framework.

The stage order encodes the design's non-negotiables:

* Authorization is resolved before retrieval and never consulted afterwards.
* The evidence gate runs *before* the model, so a refusal cannot be talked past
  and costs nothing.
* Citation validation and the output scan run *after*, on the finished text.

This is also the Phase 3 seam. When the agent layer arrives it replaces
``answer()`` — the identity, retrieval, guardrail and audit foundations
underneath stay as they are.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

from askau.core.errors import UpstreamUnavailableError
from askau.domain.answer import EvidenceAssessment, GroundedAnswer
from askau.domain.authz import AuthorizationContext
from askau.domain.enums import AnswerState, RetrievalStrategy, ts_config_for
from askau.domain.profiles import RetrievalProfile, RetrievalTier, profile_for
from askau.domain.retrieval import RetrievalQuery, RetrievalResult, RetrievedChunk
from askau.llm.ports import LLM, Embedder
from askau.llm.usage import UsageLedger
from askau.rag import citations as citation_mod
from askau.rag import clarification as clarify_mod
from askau.rag import conflict as conflict_mod
from askau.rag import context as context_mod
from askau.rag import evidence as evidence_mod
from askau.rag import followup as followup_mod
from askau.rag import grounding as grounding_mod
from askau.rag.guardrails import input_scan, output_scan
from askau.retrieval.ports import Reranker, Retriever
from askau.settings import Settings

_PROMPT_PATH = Path(__file__).parent / "prompts" / "system_v1.md"


@dataclass(frozen=True, slots=True)
class Stage:
    """A progress signal for the streaming protocol.

    Emitted so the interface can narrate the wait rather than spin: by the time
    tokens arrive the reader has already seen what was understood and which
    documents will be used.
    """

    name: str
    elapsed_ms: int


@dataclass(slots=True)
class AnswerTrace:
    """Per-request timings and diagnostics, persisted with the message."""

    #: Set when a follow-up was rewritten for retrieval (FR-004). Recorded so a
    #: surprising result can be traced to the rewrite rather than to the corpus.
    rewritten_query: str | None = None
    retrieval_ms: int = 0
    rerank_ms: int = 0
    #: Set when the evidence gate was unconvinced and a wider second pass ran.
    escalated: bool = False
    #: Candidates the database considered, summed across passes.
    candidates_considered: int = 0
    ttft_ms: int = 0
    total_ms: int = 0
    retrieved: int = 0
    reranked: int = 0
    injection_detections: tuple[str, ...] = field(default_factory=tuple)
    fabricated_markers: tuple[int, ...] = field(default_factory=tuple)
    context_tokens: int = 0


class RagOrchestrator:
    def __init__(
        self,
        *,
        retriever: Retriever,
        embedder: Embedder,
        llm: LLM,
        settings: Settings,
        reranker: Reranker | None = None,
        usage: UsageLedger | None = None,
    ) -> None:
        self._retriever = retriever
        self._embedder = embedder
        self._llm = llm
        self._reranker = reranker
        self._settings = settings
        self._usage = usage
        self._system_prompt = _PROMPT_PATH.read_text(encoding="utf-8")
        self._thresholds = evidence_mod.EvidenceThresholds(
            min_top_score=settings.min_evidence_score,
            min_supporting_chunks=settings.min_evidence_chunks,
        )

    @property
    def llm(self) -> LLM:
        """Exposed so a caller can record which model answered."""
        return self._llm

    # ── the non-streaming path (evaluation harness, future agent layer) ─────

    async def answer(
        self,
        question: str,
        authz: AuthorizationContext,
        *,
        language: str = "en",
        include_historical: bool = False,
        history: list[tuple[str, str]] | None = None,
        allow_escalation: bool = False,
    ) -> tuple[GroundedAnswer, AnswerTrace]:
        """`allow_escalation` is the caller's consent, not its instruction.

        It comes from `users.higher_intelligence` and is read at the route,
        because `rag/` may not import `db/`. Passing the *permission* rather
        than the tier keeps the decision here, where the evidence signal that
        justifies it is: a caller cannot demand a thorough pass on a question
        that did not need one.
        """
        started = time.perf_counter()
        trace = AnswerTrace()

        gate = input_scan.scan(question)
        if not gate.ok:
            return (
                GroundedAnswer(state=AnswerState.REFUSED_SAFETY, content=gate.reason),
                trace,
            )

        # FR-004. Retrieval uses the resolved query; everything the reader sees
        # — the prompt, the stored message, the audit row — uses what they
        # actually asked.
        resolution = followup_mod.resolve(question, history)
        trace.rewritten_query = resolution.query if resolution.rewritten else None
        # Embedded once, here, and reused if a second pass runs. The resolved
        # query does not change when the tier does, so re-embedding it would be
        # a network call for a value already in hand.
        vector = await self._embed(resolution.query, authz)
        retrieval = await self._retrieve(
            resolution.query, authz, language, include_historical, embedding=vector
        )
        trace.retrieval_ms = retrieval.took_ms
        trace.retrieved = len(retrieval)
        trace.candidates_considered = retrieval.candidates_considered

        chunks = await self._rerank(question, retrieval, trace)

        # FR-008, assessed BEFORE the evidence gate. Vagueness is a property of
        # the question, not of what came back: "tell me about procedures"
        # deserves a clarifying question whether retrieval found much or little,
        # and answering it with "not enough evidence" tells the reader nothing
        # they can act on. Assessed on the resolved query, so a follow-up that
        # inherited its subject is no longer too thin.
        ambiguity = clarify_mod.assess(resolution.query, retrieval)
        if ambiguity.needed:
            trace.total_ms = int((time.perf_counter() - started) * 1000)
            return (
                GroundedAnswer(
                    state=AnswerState.CLARIFICATION_NEEDED,
                    content=clarify_mod.message(ambiguity),
                    retrieval=retrieval,
                    # No evidence assessment: the gate has not run, because the
                    # question was not specific enough to be worth assessing.
                    diagnostics={"topics": ambiguity.topics},
                ),
                trace,
            )

        # ── the gate. Nothing below this line runs on insufficient evidence. ──
        retrieval, chunks, assessment = await self._assess(
            question=question,
            resolved=resolution.query,
            authz=authz,
            language=language,
            include_historical=include_historical,
            retrieval=retrieval,
            chunks=chunks,
            trace=trace,
            allow_escalation=allow_escalation,
            embedding=vector,
        )
        if not assessment.sufficient:
            trace.total_ms = int((time.perf_counter() - started) * 1000)
            return (
                GroundedAnswer(
                    state=AnswerState.INSUFFICIENT_EVIDENCE,
                    content=evidence_mod.INSUFFICIENT_EVIDENCE_MESSAGE,
                    retrieval=retrieval,
                    evidence=assessment,
                    diagnostics={"escalated": trace.escalated},
                ),
                trace,
            )

        assembled = context_mod.assemble(
            chunks,
            token_budget=self._settings.context_token_budget,
            max_chunks=self._settings.retrieval_top_k,
        )
        trace.injection_detections = assembled.injection_detections
        trace.context_tokens = assembled.tokens_used

        first_token_at = time.perf_counter()
        try:
            raw = await self._llm.complete(
                self._system_prompt, self._user_prompt(question, assembled.text)
            )
        except UpstreamUnavailableError:
            # Fails to an error, never to an ungrounded answer (ADR-0010).
            raise
        trace.ttft_ms = int((first_token_at - started) * 1000)
        await self._record_chat(
            authz, assembled.text, raw, int((time.perf_counter() - first_token_at) * 1000)
        )

        answer = self._finalize(raw, assembled, retrieval, assessment, trace)
        trace.total_ms = int((time.perf_counter() - started) * 1000)
        return answer, trace

    # ── the streaming path (the interface) ──────────────────────────────────

    async def stream(
        self,
        question: str,
        authz: AuthorizationContext,
        *,
        language: str = "en",
        include_historical: bool = False,
        history: list[tuple[str, str]] | None = None,
        allow_escalation: bool = False,
    ) -> AsyncIterator[Stage | str | GroundedAnswer]:
        """Yield progress stages, then tokens, then the finished answer.

        Sources are emitted before any token because retrieval has already
        finished by then — showing which documents will be used is real progress
        the reader can act on, unlike a spinner.
        """
        started = time.perf_counter()
        trace = AnswerTrace()

        def elapsed() -> int:
            return int((time.perf_counter() - started) * 1000)

        gate = input_scan.scan(question)
        if not gate.ok:
            yield GroundedAnswer(state=AnswerState.REFUSED_SAFETY, content=gate.reason)
            return

        yield Stage("understanding", elapsed())
        yield Stage("retrieving", elapsed())

        resolution = followup_mod.resolve(question, history)
        trace.rewritten_query = resolution.query if resolution.rewritten else None
        # Embedded once, here, and reused if a second pass runs. The resolved
        # query does not change when the tier does, so re-embedding it would be
        # a network call for a value already in hand.
        vector = await self._embed(resolution.query, authz)
        retrieval = await self._retrieve(
            resolution.query, authz, language, include_historical, embedding=vector
        )
        trace.retrieval_ms = retrieval.took_ms
        trace.retrieved = len(retrieval)
        trace.candidates_considered = retrieval.candidates_considered
        chunks = await self._rerank(question, retrieval, trace)

        # Same order as the buffered path: clarification first, because vagueness
        # is a property of the question rather than of what came back.
        ambiguity = clarify_mod.assess(resolution.query, retrieval)
        if ambiguity.needed:
            trace.total_ms = elapsed()
            yield GroundedAnswer(
                state=AnswerState.CLARIFICATION_NEEDED,
                content=clarify_mod.message(ambiguity),
                retrieval=retrieval,
                diagnostics={"topics": ambiguity.topics},
            )
            return

        retrieval, chunks, assessment = await self._assess(
            question=question,
            resolved=resolution.query,
            authz=authz,
            language=language,
            include_historical=include_historical,
            retrieval=retrieval,
            chunks=chunks,
            trace=trace,
            allow_escalation=allow_escalation,
            embedding=vector,
        )
        if not assessment.sufficient:
            trace.total_ms = elapsed()
            yield GroundedAnswer(
                state=AnswerState.INSUFFICIENT_EVIDENCE,
                content=evidence_mod.INSUFFICIENT_EVIDENCE_MESSAGE,
                retrieval=retrieval,
                evidence=assessment,
            )
            return

        assembled = context_mod.assemble(
            chunks,
            token_budget=self._settings.context_token_budget,
            max_chunks=self._settings.retrieval_top_k,
        )
        trace.injection_detections = assembled.injection_detections
        trace.context_tokens = assembled.tokens_used

        # The `sources` signal: emitted before generation begins.
        yield GroundedAnswer(
            state=AnswerState.GROUNDED,
            content="",
            retrieval=RetrievalResult(
                chunks=tuple(assembled.chunks_by_marker.values()),
                strategy=retrieval.strategy,
                candidates_considered=retrieval.candidates_considered,
                took_ms=retrieval.took_ms,
            ),
            diagnostics={"phase": "sources"},
        )
        yield Stage("composing", elapsed())

        buffer: list[str] = []
        first = True
        async for piece in self._llm.stream(
            self._system_prompt, self._user_prompt(question, assembled.text)
        ):
            if piece.is_final:
                break
            if first:
                trace.ttft_ms = elapsed()
                first = False
            buffer.append(piece.text)
            yield piece.text

        trace.total_ms = elapsed()
        completion = "".join(buffer)
        await self._record_chat(
            authz, assembled.text, completion, max(0, trace.total_ms - trace.ttft_ms)
        )
        yield self._finalize(completion, assembled, retrieval, assessment, trace)

    # ── stages ──────────────────────────────────────────────────────────────

    async def _assess(
        self,
        *,
        question: str,
        resolved: str,
        authz: AuthorizationContext,
        language: str,
        include_historical: bool,
        retrieval: RetrievalResult,
        chunks: tuple[RetrievedChunk, ...],
        trace: AnswerTrace,
        allow_escalation: bool,
        embedding: list[float] | None = None,
    ) -> tuple[RetrievalResult, tuple[RetrievedChunk, ...], EvidenceAssessment]:
        """Run the evidence gate, widening once if it is unconvinced (FR-016).

        Shared by the buffered and streaming paths deliberately. The two have
        drifted apart before — there were once two `_sse` helpers that
        serialized differently — and a gate that escalates on one path but not
        the other would be the worst version of that: the same question
        answered or refused depending on which endpoint the client used.

        The second pass is only reached when the gate was unconvinced *and*
        this person consented. Widening after a weak result costs nothing on
        the answers that were already grounded, and the signal it uses — this
        question's own evidence — is a better judge of "complex" than anything
        guessable before retrieving.

        Re-assessed afterwards on its own merits: a wider net that still finds
        nothing is still a refusal. Escalation buys another look, never a
        lower bar.
        """

        def gate(r: RetrievalResult, c: tuple[RetrievedChunk, ...]) -> EvidenceAssessment:
            return evidence_mod.assess(
                RetrievalResult(
                    chunks=c,
                    strategy=r.strategy,
                    candidates_considered=r.candidates_considered,
                    took_ms=r.took_ms,
                ),
                self._thresholds,
                # The resolved query, not the surface form. "Does this apply to
                # staff on probation?" shares almost no vocabulary with the
                # leave policy on its own, so judging evidence against the
                # elliptical question refuses a follow-up whose answer was
                # sitting right there.
                resolved,
            )

        assessment = gate(retrieval, chunks)
        if assessment.sufficient or not allow_escalation:
            return retrieval, chunks, assessment

        wider = await self._retrieve(
            resolved,
            authz,
            language,
            include_historical,
            self._profile(RetrievalTier.THOROUGH),
            embedding=embedding,
        )
        trace.escalated = True
        trace.retrieval_ms += wider.took_ms
        trace.candidates_considered += wider.candidates_considered
        # Only adopt the wider result if it actually found more. A second pass
        # that returns the same chunks has nothing to re-assess, and swapping in
        # an equal-or-worse result would let escalation make an answer worse.
        if len(wider) <= len(retrieval):
            return retrieval, chunks, assessment

        chunks = await self._rerank(question, wider, trace)
        trace.retrieved = len(wider)
        return wider, chunks, gate(wider, chunks)

    def _profile(self, tier: RetrievalTier) -> RetrievalProfile:
        """The tier resolved against configuration. See `domain/profiles.py`."""
        return profile_for(
            tier,
            candidate_k=self._settings.retrieval_candidate_k,
            top_k=self._settings.retrieval_top_k,
            rerank_input_k=self._settings.rerank_input_k,
        )

    async def _retrieve(
        self,
        question: str,
        authz: AuthorizationContext,
        language: str,
        include_historical: bool,
        profile: RetrievalProfile | None = None,
        embedding: list[float] | None = None,
    ) -> RetrievalResult:
        """`embedding` is reusable across passes because it is a pure function
        of the query text, which does not change when the tier does.

        Worth passing explicitly rather than caching inside `_embed`: the second
        pass would otherwise make a network call to the embedding model for a
        result it already holds, and bill it to `model_invocations` as though it
        were work.
        """
        p = profile or self._profile(RetrievalTier.STANDARD)
        vector = embedding if embedding is not None else await self._embed(question, authz)
        return await self._retriever.search(
            RetrievalQuery(
                text=question,
                embedding=vector,
                authz=authz,
                candidate_k=p.candidate_k,
                top_k=p.top_k,
                strategy=RetrievalStrategy.HYBRID,
                include_historical=include_historical,
                # The *asker's* configuration. Documents are stemmed with their
                # own; cross-language matching is the semantic arm's job.
                ts_config=ts_config_for(language),
            )
        )

    async def _embed(self, question: str, authz: AuthorizationContext) -> list[float]:
        if self._usage is None:
            return await self._embedder.embed_query(question)
        with self._usage.measure(
            "embed_query",
            self._settings.embedding_provider,
            self._embedder.model_name,
            user_id=str(authz.user_id),
            department=authz.department,
        ) as m:
            embedding = await self._embedder.embed_query(question)
            m.tokens(input_tokens=max(1, len(question) // 4))
        return embedding

    async def _record_chat(
        self,
        authz: AuthorizationContext,
        prompt: str,
        completion: str,
        latency_ms: int,
    ) -> None:
        if self._usage is None:
            return
        from askau.llm.usage import Invocation

        self._usage.record(
            Invocation(
                operation="chat",
                provider=self._llm.provider,
                model=self._llm.model_name,
                input_tokens=max(1, len(prompt) // 4),
                output_tokens=max(1, len(completion) // 4),
                # FR-053 names LLM latency explicitly. Recorded here rather than
                # taken from the trace so it survives the streaming path, where
                # the trace only carries time-to-first-token.
                latency_ms=latency_ms,
                user_id=str(authz.user_id),
                department=authz.department,
            )
        )

    async def _rerank(self, question: str, retrieval: RetrievalResult, trace: AnswerTrace) -> tuple:  # type: ignore[type-arg]
        if self._reranker is None or retrieval.is_empty:
            return retrieval.top(self._settings.retrieval_top_k)
        started = time.perf_counter()
        reranked = await self._reranker.rerank(
            question, list(retrieval.chunks), self._settings.retrieval_top_k
        )
        trace.rerank_ms = int((time.perf_counter() - started) * 1000)
        trace.reranked = len(reranked)
        return tuple(reranked)

    def _finalize(
        self,
        raw: str,
        assembled: context_mod.AssembledContext,
        retrieval: RetrievalResult,
        assessment: EvidenceAssessment,
        trace: AnswerTrace,
    ) -> GroundedAnswer:
        """Post-generation validation. Nothing here trusts the model."""
        scan_result = output_scan.scan(raw, assembled.chunks_by_marker)
        if scan_result.blocked:
            return GroundedAnswer(
                state=AnswerState.REFUSED_SAFETY,
                content=output_scan.REFUSED_SAFETY_MESSAGE,
                retrieval=retrieval,
                diagnostics={"blocked_for": scan_result.reasons},
            )

        resolved = citation_mod.build_citations(scan_result.text, assembled.chunks_by_marker)
        trace.fabricated_markers = resolved.fabricated_markers

        grounding = grounding_mod.score(resolved.text, assembled.text)

        # A topic only counts as in play when the answer cited something in it —
        # otherwise a per-diem disagreement would attach itself to a question
        # about annual leave, and a notice that fires on unrelated questions is
        # one users learn to ignore.
        #
        # But once a topic *is* in play, every retrieved chunk on it is compared,
        # not just the cited ones. Comparing only citations let the model decide
        # whether a disagreement was disclosed: quoting both figures warned the
        # reader, quoting one and ignoring the other did not, and which happened
        # varied run to run on identical input.
        cited_markers = {c.marker for c in resolved.citations}
        cited_chunks = tuple(
            chunk for marker, chunk in assembled.chunks_by_marker.items() if marker in cited_markers
        )
        retrieved_chunks = tuple(assembled.chunks_by_marker.values())
        conflicts = conflict_mod.detect(cited_chunks or retrieved_chunks, retrieved_chunks)

        if conflicts:
            state = AnswerState.CONFLICT
        elif grounding.is_grounded:
            state = AnswerState.GROUNDED
        elif grounding.is_partial:
            state = AnswerState.PARTIALLY_GROUNDED
        else:
            # Generated text that its own sources do not support is a refusal,
            # not a low-confidence answer.
            return GroundedAnswer(
                state=AnswerState.INSUFFICIENT_EVIDENCE,
                content=evidence_mod.INSUFFICIENT_EVIDENCE_MESSAGE,
                retrieval=retrieval,
                evidence=assessment,
                groundedness=grounding.score,
            )

        return GroundedAnswer(
            state=state,
            content=resolved.text,
            retrieval=RetrievalResult(
                chunks=tuple(assembled.chunks_by_marker.values()),
                strategy=retrieval.strategy,
                candidates_considered=retrieval.candidates_considered,
                took_ms=retrieval.took_ms,
            ),
            conflicts=conflicts,
            groundedness=grounding.score,
            evidence=assessment,
            diagnostics={
                "citations": resolved.citations,
                "fabricated_markers": resolved.fabricated_markers,
                "unused_sources": resolved.unused_sources,
                "authority_scrubbed": scan_result.scrubbed,
                "injection_detections": assembled.injection_detections,
                # The sentences the lexical measure could not match. Carried
                # because the score alone is not interpretable: an answer that
                # drifted from its sources and an answer that explained which
                # source it preferred both land below 1.0, and only these
                # sentences tell you which happened.
                "unsupported_sentences": grounding.unsupported,
            },
        )

    def _user_prompt(self, question: str, context: str) -> str:
        return (
            f"## Retrieved AUC sources\n\n{context}\n\n"
            f"## Question\n\n{question}\n\n"
            "Answer using only the sources above, citing each claim."
        )
