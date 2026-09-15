"""The OpenAI-compatible LLM adapter, against a stubbed server.

This adapter is the one that will talk to whatever AUC self-hosts — vLLM,
Ollama, or a managed endpoint — and until now it had never been executed. Not
once, by anything. `ASKAU_LLM_PROVIDER` has been `echo` throughout development,
so a mistake in the request shape or the stream parsing would have been found at
deployment, against a real endpoint, by somebody else.

A stubbed transport rather than a live model: what needs proving here is the
*protocol* — the request we send, the response we parse, and what happens when
the endpoint misbehaves. Whether the answers are any good is a different
question and needs a different tool (`make eval-real`).
"""

from __future__ import annotations

import json

import httpx
import pytest

from askau.core.errors import UpstreamUnavailableError
from askau.llm.adapters.openai_compatible import OpenAICompatibleLLM


def _llm(handler) -> OpenAICompatibleLLM:  # type: ignore[no-untyped-def]
    return OpenAICompatibleLLM(
        base_url="http://model.invalid/v1",
        model="test-model",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


class TestTheRequestWeSend:
    async def test_shape_matches_the_openai_schema(self) -> None:
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(json.loads(request.content))
            seen["path"] = request.url.path
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

        await _llm(handler).complete("system prompt", "user question")

        assert seen["path"] == "/v1/chat/completions"
        assert seen["model"] == "test-model"
        assert seen["messages"] == [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "user question"},
        ]

    async def test_temperature_is_low_but_not_zero(self) -> None:
        """Grounded extraction should be near-deterministic, but exactly 0 makes
        some servers degenerate into repetition loops on long contexts — and our
        contexts are long by construction."""
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

        await _llm(handler).complete("s", "u")
        assert 0 < float(seen["temperature"]) <= 0.2, seen["temperature"]

    async def test_the_token_budget_is_sent(self) -> None:
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

        await _llm(handler).complete("s", "u", max_tokens=123)
        assert seen["max_tokens"] == 123


class TestStreaming:
    async def test_deltas_are_yielded_in_order(self) -> None:
        frames = [
            'data: {"choices":[{"delta":{"content":"The "}}]}',
            'data: {"choices":[{"delta":{"content":"daily "}}]}',
            'data: {"choices":[{"delta":{"content":"rate"}}]}',
            "data: [DONE]",
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="\n\n".join(frames))

        chunks = [c async for c in _llm(handler).stream("s", "u")]
        assert "".join(c.text for c in chunks) == "The daily rate"
        assert chunks[-1].is_final

    async def test_keepalives_and_malformed_frames_are_skipped(self) -> None:
        """Real servers interleave comments and the occasional partial frame.
        Dropping one is correct; crashing the answer is not."""
        frames = [
            ": keep-alive",
            'data: {"choices":[{"delta":{"content":"a"}}]}',
            "data: {not json",
            'data: {"choices":[{"delta":{}}]}',
            'data: {"choices":[{"delta":{"content":"b"}}]}',
            "data: [DONE]",
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="\n\n".join(frames))

        chunks = [c async for c in _llm(handler).stream("s", "u")]
        assert "".join(c.text for c in chunks) == "ab"

    async def test_nothing_after_done(self) -> None:
        """A server that keeps talking past [DONE] must not extend the answer."""
        frames = [
            'data: {"choices":[{"delta":{"content":"answer"}}]}',
            "data: [DONE]",
            'data: {"choices":[{"delta":{"content":" MORE"}}]}',
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="\n\n".join(frames))

        chunks = [c async for c in _llm(handler).stream("s", "u")]
        assert "MORE" not in "".join(c.text for c in chunks)


class TestItFailsRatherThanInvents:
    """ADR-0010, and the single most important property of this adapter.

    When the model is unreachable the answer must not fall back to model
    knowledge or to an empty-but-confident reply. It must fail. An AskAU that
    answers from a language model's memory when retrieval or generation is down
    is exactly the system FR-033 and BR-007 forbid.
    """

    @pytest.mark.parametrize("status", [400, 401, 429, 500, 502, 503])
    async def test_every_error_status_raises(self, status: int) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json={"error": "nope"})

        with pytest.raises(UpstreamUnavailableError):
            await _llm(handler).complete("s", "u")

    async def test_a_connection_failure_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        with pytest.raises(UpstreamUnavailableError):
            await _llm(handler).complete("s", "u")

    async def test_a_malformed_success_body_raises(self) -> None:
        """A 200 carrying something we cannot read is not an answer.

        Returning "" here would be worse than failing: an empty answer renders
        as a confident blank rather than as the outage it is.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"unexpected": "shape"})

        with pytest.raises(UpstreamUnavailableError):
            await _llm(handler).complete("s", "u")

    async def test_streaming_failure_raises_too(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="")

        with pytest.raises(UpstreamUnavailableError):
            [c async for c in _llm(handler).stream("s", "u")]

    async def test_the_error_never_carries_the_upstream_message(self) -> None:
        """Whatever the endpoint says about itself is not for the reader — it can
        carry an internal hostname, a key fragment, or a stack."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="postgres://admin:hunter2@internal-host/db failed")

        with pytest.raises(UpstreamUnavailableError) as caught:
            await _llm(handler).complete("s", "u")
        assert "hunter2" not in str(caught.value)
        assert "internal-host" not in str(caught.value)
