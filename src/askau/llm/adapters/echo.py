"""Deterministic generation for development, CI and evaluation baselines.

Produces a grounded-looking answer assembled from the retrieved context, with
citation markers that resolve to real chunks. That matters: the citation
validator, the grounding scorer and the streaming protocol all need something to
act on, and a provider that returned lorem ipsum would let those stages pass
vacuously.

It is not a language model. It cannot paraphrase, reason or refuse — those
behaviours are exercised against a real provider. What it gives is a pipeline
that runs end to end with no credentials and identical output every time.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator

from askau.llm.ports import CompletionChunk

_SENTENCE = re.compile(r"(?<=[.!?])\s+")
#: Matches one shielded source block and captures its marker and inner body.
#: Anchored on the envelope delimiters so the delimiters themselves never leak
#: into the answer — which is exactly what a careless regex did first time.
_ENVELOPE = re.compile(
    # [^\n]* rather than [^>]*: the latter also matches newlines, so with several
    # envelopes present it spans across blocks before backtracking — invisible
    # when tested against a single envelope, wrong on every real prompt.
    r"<<<SOURCE (\d+) BEGIN[^\n]*>>>\n"  # opening delimiter + marker
    r"\[\d+\][^\n]*\n"  # the "[n] Title — locator" header
    r"(.*?)"  # body
    r"\n<<<SOURCE \1 END>>>",
    re.S,
)


class EchoLLM:
    """Assembles an answer from the context block the orchestrator supplies."""

    def __init__(self, model_name: str = "echo-1", delay_ms: int = 0) -> None:
        self._model = model_name
        self._delay = delay_ms / 1000.0

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def provider(self) -> str:
        return "echo"

    async def complete(self, system: str, user: str, *, max_tokens: int = 800) -> str:
        return _compose(user, max_tokens)

    async def stream(
        self, system: str, user: str, *, max_tokens: int = 800
    ) -> AsyncIterator[CompletionChunk]:
        text = _compose(user, max_tokens)
        # Emit in word groups so the client's token batching is genuinely
        # exercised rather than receiving one blob.
        words = text.split(" ")
        for i in range(0, len(words), 6):
            piece = " ".join(words[i : i + 6])
            if i + 6 < len(words):
                piece += " "
            if self._delay:
                await asyncio.sleep(self._delay)
            yield CompletionChunk(piece)
        yield CompletionChunk("", is_final=True)


def _compose(user_prompt: str, max_tokens: int) -> str:
    """Build an answer from the numbered source blocks in the prompt.

    Deliberately extractive: it quotes the retrieved text rather than inventing
    around it, so groundedness scoring sees a genuinely grounded answer and any
    regression in the scorer shows up as a falling score rather than a passing one.
    """
    blocks = _ENVELOPE.findall(user_prompt)
    if not blocks:
        return (
            "I could not locate supporting material for that question in the "
            "approved AUC knowledge sources available to you."
        )

    lines: list[str] = []
    # Cite a handful of sources rather than all of them. Citing every source
    # supplied would make groundedness trivially 1.0 and hide any regression in
    # the scorer.
    budget = min(3, max(1, max_tokens // 250))
    for marker, body in blocks[:budget]:
        sentences = [s.strip() for s in _SENTENCE.split(body.strip()) if s.strip()]
        if not sentences:
            continue
        excerpt = " ".join(sentences[:2])
        lines.append(f"{excerpt} [{marker}]")

    if not lines:
        return "The retrieved sources did not contain usable text for that question."
    return "\n\n".join(lines)
