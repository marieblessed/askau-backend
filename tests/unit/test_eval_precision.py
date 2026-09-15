"""Retrieval precision — the metric the gate was blind to.

Before this existed the evaluation gate reported 100% pass rate and 100%
groundedness against a hash embedder whose precision was 18%: four of every five
retrieved documents were noise. Both of the old numbers were true and neither
was the whole picture — an answer can be perfectly grounded in three sources
when only one of them was relevant, because groundedness asks whether the answer
follows from what was retrieved, not whether what was retrieved belonged.

That gap is why this is a metric rather than a comment.
"""

from __future__ import annotations

from askau.evaluation.runner import QuestionResult, _precision


def _result(qid: str, retrieved: tuple[str, ...], expected: tuple[str, ...]) -> QuestionResult:
    return QuestionResult(
        question_id=qid,
        question="q",
        passed=True,
        actual_state="grounded",
        retrieved_families=retrieved,
        expected_families=expected,
    )


class TestPrecision:
    def test_everything_retrieved_was_wanted(self) -> None:
        assert _precision([_result("a", ("x",), ("x",))]) == 1.0

    def test_noise_lowers_it(self) -> None:
        """One wanted document among four is 25%, whatever the answer said."""
        assert _precision([_result("a", ("x", "n1", "n2", "n3"), ("x",))]) == 0.25

    def test_missing_the_right_document_is_zero(self) -> None:
        assert _precision([_result("a", ("n1", "n2"), ("x",))]) == 0.0

    def test_averaged_per_question_not_pooled(self) -> None:
        """A question retrieving twenty sources must not drown out one retrieving two.

        Pooled over documents these two would give 3/22 ≈ 14%. Per question they
        give (1/20 + 1/2) / 2 ≈ 27%, which is the honest reading: one question
        did well and one did badly.
        """
        results = [
            _result("noisy", (*(f"n{i}" for i in range(19)), "x"), ("x",)),
            _result("clean", ("y", "n"), ("y",)),
        ]
        value = _precision(results)
        assert value is not None
        assert 0.26 < value < 0.28, value

    def test_questions_without_expectations_are_excluded(self) -> None:
        """Not every question declares what it should retrieve — a refusal test
        has nothing to be precise about. Counting those as zero would make the
        metric a measure of how many questions declare expectations."""
        results = [
            _result("declared", ("x",), ("x",)),
            _result("undeclared", ("whatever",), ()),
        ]
        assert _precision(results) == 1.0

    def test_none_when_nothing_can_be_scored(self) -> None:
        """`None`, not 0.0 or 1.0 — either would be a claim, and the honest
        statement is that the question was not asked."""
        assert _precision([]) is None
        assert _precision([_result("a", (), ())]) is None
