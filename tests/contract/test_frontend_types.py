"""Our wire models against the frontend's TypeScript types.

The client (`askau-frontend`) makes no live calls yet — every screen renders
fixtures — so nothing on its side would fail if our shapes drifted. Its types
are hand-written and are what its whole application is built on, which makes
them the contract and makes this the only test that can catch a divergence
before their Phase 2 wiring does.

It reads their `.ts` files directly rather than a copied snapshot. A snapshot
would agree with itself forever; the point is to notice when *they* change
something.

Skipped when the frontend is not checked out beside this repo, the same way the
database tests skip without a database. A skip is honest here — the check simply
cannot run — whereas passing would claim an alignment nobody verified.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from askau.api.schemas import wire

#: Sibling checkout by default; overridable so CI can point at a clone.
_FRONTEND = Path(
    os.environ.get(
        "ASKAU_FRONTEND_PATH",
        Path(__file__).resolve().parents[4] / "askau-frontend",
    )
)

requires_frontend = pytest.mark.skipif(
    not (_FRONTEND / "types").is_dir(),
    reason=f"askau-frontend not found at {_FRONTEND}; set ASKAU_FRONTEND_PATH",
)

pytestmark = [requires_frontend]


def _interface_fields(relative: str, name: str) -> set[str]:
    """Field names declared on one TypeScript interface.

    Deliberately a regex and not a parser: the alternative is a Node dependency
    in the Python test suite to read four files. It handles what their types
    actually contain — flat interfaces of `name: Type;` and `name?: Type;` — and
    would need replacing if they adopted generics or nested object literals in
    these particular shapes.
    """
    source = (_FRONTEND / relative).read_text()
    match = re.search(
        rf"export interface {name}(?:<[^>]*>)?\s*\{{(.*?)^\}}",
        source,
        re.S | re.M,
    )
    assert match, f"interface {name} not found in {relative} — did it get renamed?"

    body = match.group(1)
    # Strip comments so a field name mentioned in prose is not counted.
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    body = re.sub(r"//[^\n]*", "", body)
    return set(re.findall(r"^\s*(\w+)\??\s*:", body, re.M))


def _wire_aliases(model: type) -> set[str]:
    """The keys our model actually serializes, as the client would receive them."""
    return {
        (field.alias or name)
        for name, field in model.model_fields.items()  # type: ignore[attr-defined]
    }


class TestTheClientCanParseWhatWeSend:
    """Every field the client declares must exist in our payload.

    One-directional on purpose. We may send *more* than they read — `maxClassification`
    on the user, `conversationId` on a message — and extra fields are harmless to
    a TypeScript consumer. A *missing* field is what breaks a render, so that is
    what this asserts.
    """

    @pytest.mark.parametrize(
        ("relative", "interface", "model", "known_differences"),
        [
            ("types/conversation.ts", "Conversation", wire.ConversationOut, set()),
            ("types/api.ts", "HealthResponse", wire.HealthResponse, set()),
            (
                "types/auth.ts",
                "User",
                wire.UserOut,
                set(),
            ),
            (
                "features/chat/types/index.ts",
                "Source",
                wire.SourceOut,
                set(),
            ),
            (
                "features/chat/types/index.ts",
                "Message",
                wire.MessageOut,
                # Their two models of a message disagree with each other:
                # `features/chat/types/index.ts` calls this `timestamp` while
                # `types/chat.ts` — their own declared API contract — calls it
                # `createdAt`, as does every other type they have. We send
                # `createdAt` and let their component do the `new Date(...)` it
                # already does. Recorded here so the difference is a decision
                # rather than a gap.
                {"timestamp"},
            ),
        ],
        ids=["Conversation", "HealthResponse", "User", "Source", "Message"],
    )
    def test_no_declared_field_is_missing(
        self, relative: str, interface: str, model: type, known_differences: set[str]
    ) -> None:
        declared = _interface_fields(relative, interface)
        ours = _wire_aliases(model)
        missing = declared - ours - known_differences
        assert not missing, (
            f"{interface} ({relative}) declares fields our {model.__name__} does not send: "
            f"{sorted(missing)}"
        )


class TestEnvelopeAndVocabulary:
    def test_list_envelope_matches(self) -> None:
        declared = _interface_fields("types/api.ts", "ListResponse")
        ours = _wire_aliases(wire.ListResponse)
        assert declared == ours, (
            f"pagination envelope differs — theirs {sorted(declared)}, ours {sorted(ours)}"
        )

    def test_classification_vocabulary_matches(self) -> None:
        """Their `ClassLevel` is upper-case; our enum is lower-case throughout.

        The conversion happens once, in `api/mappers.py`. This asserts the two
        vocabularies still describe the same four levels — a fifth tier added on
        either side would otherwise surface as an unstyled badge.
        """
        from askau.domain.enums import Classification

        source = (_FRONTEND / "features/chat/types/index.ts").read_text()
        match = re.search(r"export type ClassLevel =([^;]+);", source)
        assert match, "ClassLevel not found"
        theirs = set(re.findall(r'"(\w+)"', match.group(1)))
        ours = {c.value.upper() for c in Classification}
        assert theirs == ours, f"theirs {sorted(theirs)}, ours {sorted(ours)}"

    def test_role_vocabulary_is_the_clients(self) -> None:
        """`/users/me` must speak their three-tier role vocabulary.

        Their `permissions.ts` compares roles by *index* into an ordered
        hierarchy, so a value it does not know ranks below `user` and silently
        locks someone out of their own interface.
        """
        from askau.api.v1.routes.auth import _ROLE_DISPLAY

        source = (_FRONTEND / "types/auth.ts").read_text()
        match = re.search(r"export type UserRole =([^;]+);", source)
        assert match, "UserRole not found"
        theirs = set(re.findall(r'"(\w+)"', match.group(1)))
        assert set(_ROLE_DISPLAY.values()) <= theirs, (
            f"we emit roles they do not declare: {sorted(set(_ROLE_DISPLAY.values()) - theirs)}"
        )


class TestAnswerStates:
    """Their `MessageState` and our `AnswerState` do not match, by design.

    We produce three states their UI has no branch for — clarification,
    refusal and out-of-scope — and collapsing them into `error` would report a
    refusal as a fault. They render one, `outdated`, that we cannot yet produce.

    This test does not demand they agree. It pins the *difference*, so that when
    either side adds a state the mismatch surfaces here rather than as an
    unstyled panel in front of a user.
    """

    def test_the_known_gap_has_not_changed(self) -> None:
        from askau.domain.enums import AnswerState

        source = (_FRONTEND / "features/chat/types/index.ts").read_text()
        match = re.search(r"export type MessageState =([^;]+);", source)
        assert match, "MessageState not found"
        theirs = set(re.findall(r'"([\w-]+)"', match.group(1)))

        # What we emit that they cannot render.
        ours_only = {
            AnswerState.CLARIFICATION_NEEDED.value,
            AnswerState.REFUSED_SAFETY.value,
            AnswerState.OUT_OF_SCOPE.value,
            AnswerState.PARTIALLY_GROUNDED.value,
            AnswerState.INSUFFICIENT_EVIDENCE.value,
            AnswerState.CONFLICT.value,
        }
        assert ours_only.isdisjoint(theirs), (
            "the client now declares a state we thought it did not — reconcile "
            f"the mapping: {sorted(ours_only & theirs)}"
        )

        # What they render that nothing produces yet.
        assert "outdated" in theirs, (
            "the client dropped `outdated`; we were planning to start producing it"
        )


class TestStateMappingIsTotalAndSafe:
    """`state` must always be a value their UI can render.

    `features/chat/components/ai-message.tsx` is a chain of independent
    `message.state === "x" &&` blocks with no default. An unrecognised value
    renders an **empty message** — not a fallback, not the answer text. So a
    partial mapping is not a cosmetic problem, it is a blank answer.
    """

    def test_every_state_we_can_emit_maps_to_one_they_render(self) -> None:
        from askau.api.mappers import answer_state_display
        from askau.domain.enums import AnswerState

        source = (_FRONTEND / "features/chat/types/index.ts").read_text()
        match = re.search(r"export type MessageState =([^;]+);", source)
        assert match, "MessageState not found"
        renderable = set(re.findall(r'"([\w-]+)"', match.group(1)))

        for state in AnswerState:
            mapped = answer_state_display(state.value)
            assert mapped in renderable, (
                f"{state.value} maps to {mapped!r}, which their UI cannot render — "
                "it would show an empty message"
            )

    def test_the_states_their_ui_drops_our_text_for_are_recorded(self) -> None:
        """A guard on a known, deliberate loss.

        Their `insufficient` branch renders fixed translated copy and never
        `message.content`, so for these three our explanation of why we did not
        answer is discarded — and FR-028 requires us to state exactly that.
        Mapping them anywhere else is worse (a blank message, or a false
        "error"), so the fix belongs in their UI. This test fails if the set
        changes, so the note stays true.
        """
        from askau.api.mappers import _STATE_DISPLAY

        text_is_dropped = {
            state
            for state, shown in _STATE_DISPLAY.items()
            if shown == "insufficient" and state != "insufficient_evidence"
        }
        assert text_is_dropped == {
            "clarification_needed",
            "out_of_scope",
            "refused_safety",
        }, f"the set of states whose explanation is dropped changed: {sorted(text_is_dropped)}"

    def test_content_bearing_branches_are_still_content_bearing(self) -> None:
        """If they ever render `message.content` in the insufficient branch, the
        loss above is closed and this test says so."""
        source = (_FRONTEND / "features/chat/components/ai-message.tsx").read_text()
        insufficient = re.search(
            r'message\.state === "insufficient" &&(.*?)\n      \)\}', source, re.S
        )
        assert insufficient, "could not locate the insufficient branch"
        if "message.content" in insufficient.group(1):
            pytest.fail(
                "their insufficient branch now renders message.content — remove the "
                "note in mappers.py about our explanation being dropped, and consider "
                "mapping refusals and clarifications to distinct states"
            )
