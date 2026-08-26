"""Language configuration mapping — ADR-0015.

The defect this guards against does not raise: stemming French with the English
configuration produces poor tokens silently.
"""

from __future__ import annotations

import pytest

from askau.domain.enums import (
    DEFAULT_TS_CONFIG,
    RETRIEVABLE_LIFECYCLES,
    AnswerState,
    Lifecycle,
    ts_config_for,
)


class TestTextSearchConfig:
    @pytest.mark.parametrize(
        ("language", "expected"),
        [
            ("en", "english"),
            ("fr", "french"),
            ("ar", "arabic"),
            ("pt", "portuguese"),
            ("es", "spanish"),
            ("en-GB", "english"),
            ("FR", "french"),
        ],
    )
    def test_known_languages_map_to_their_stemmer(self, language: str, expected: str) -> None:
        assert ts_config_for(language) == expected

    @pytest.mark.parametrize("language", ["sw", "am", "ti", "zu", "xx"])
    def test_languages_without_a_postgres_stemmer_fall_back_to_simple(self, language: str) -> None:
        """Kiswahili and Amharic have no Postgres stemmer. Guessing one is worse
        than not stemming — it produces wrong tokens without erroring."""
        assert ts_config_for(language) == DEFAULT_TS_CONFIG

    @pytest.mark.parametrize("language", [None, ""])
    def test_unknown_language_never_defaults_to_english(self, language: str | None) -> None:
        assert ts_config_for(language) == DEFAULT_TS_CONFIG


class TestLifecycle:
    def test_expired_and_superseded_are_not_retrievable(self) -> None:
        """FR-018 — outdated policy must not be surfaced by default."""
        assert Lifecycle.EXPIRED not in RETRIEVABLE_LIFECYCLES
        assert Lifecycle.SUPERSEDED not in RETRIEVABLE_LIFECYCLES
        assert Lifecycle.DRAFT not in RETRIEVABLE_LIFECYCLES

    def test_active_is_retrievable(self) -> None:
        assert Lifecycle.ACTIVE in RETRIEVABLE_LIFECYCLES


class TestAnswerState:
    @pytest.mark.parametrize(
        "state",
        [AnswerState.GROUNDED, AnswerState.PARTIALLY_GROUNDED, AnswerState.CONFLICT],
    )
    def test_answered_states(self, state: AnswerState) -> None:
        assert state.is_answered

    @pytest.mark.parametrize(
        "state",
        [
            AnswerState.INSUFFICIENT_EVIDENCE,
            AnswerState.OUT_OF_SCOPE,
            AnswerState.CLARIFICATION_NEEDED,
            AnswerState.REFUSED_SAFETY,
            AnswerState.ERROR,
        ],
    )
    def test_refusal_states_are_not_answers(self, state: AnswerState) -> None:
        assert not state.is_answered
