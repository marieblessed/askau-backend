"""Follow-up resolution — FR-004.

The failure this guards against is subtle: concatenating the previous question
onto the current one makes every earlier keyword compete in the search, so a
follow-up about probation retrieves the travel policy because the last turn
mentioned travel.
"""

from __future__ import annotations

import pytest

from askau.rag.followup import resolve

LEAVE = [
    ("user", "What is the annual leave entitlement?"),
    ("assistant", "Staff accrue thirty working days per calendar year."),
]


class TestReferringFollowUps:
    @pytest.mark.parametrize(
        "question",
        [
            "Does this apply to staff on probation?",
            "What about carry-over?",
            "Is that the same for part-time staff?",
            "Does it include public holidays?",
        ],
    )
    def test_inherit_the_subject(self, question: str) -> None:
        result = resolve(question, LEAVE)
        assert result.rewritten
        assert "annual" in result.query and "leave" in result.query
        # The follow-up's own words survive — it narrows within the subject.
        assert question in result.query

    def test_subject_comes_from_the_question_not_the_answer(self) -> None:
        """An answer is long, and its incidental vocabulary would swamp the
        follow-up it is meant to support."""
        result = resolve("Does this apply to probation?", LEAVE)
        assert "calendar" not in result.query
        assert "accrue" not in result.query


class TestSelfContainedQuestions:
    @pytest.mark.parametrize(
        "question",
        [
            "What is the daily subsistence allowance for continental travel?",
            "What documents are required for vendor onboarding?",
            "Per Diem Rates Circular 2025/04",
        ],
    )
    def test_are_left_alone(self, question: str) -> None:
        """Rewriting a question that did not need it is how a working search
        turns into a mysterious one."""
        result = resolve(question, LEAVE)
        assert not result.rewritten
        assert result.query == question

    def test_a_long_question_is_never_treated_as_a_follow_up(self) -> None:
        long_q = (
            "What about the specific documentation requirements that apply when "
            "onboarding a new vendor for procurement purposes?"
        )
        assert not resolve(long_q, LEAVE).rewritten


class TestBoundaries:
    def test_no_history_means_no_rewrite(self) -> None:
        assert not resolve("Does this apply to probation?", None).rewritten
        assert not resolve("Does this apply to probation?", []).rewritten

    def test_history_with_no_user_turn_is_ignored(self) -> None:
        assert not resolve("Does this apply?", [("assistant", "Some answer.")]).rewritten

    def test_subject_survives_across_an_intervening_answer(self) -> None:
        history = [
            *LEAVE,
            ("user", "What is the travel per diem?"),
            ("assistant", "USD 180 per night."),
        ]
        # The most recent question wins, not the first.
        result = resolve("Does this apply to probation?", history)
        assert "travel" in result.query or "diem" in result.query
