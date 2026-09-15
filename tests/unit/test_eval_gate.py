"""The acceptance gate (§6.8).

The groundedness threshold was calibrated against a stub. `echo` returns
retrieved text verbatim, so it scores 1.00 on every question by construction —
the 90% mean bar was never once measured against a model that paraphrases, and
the first real one produced 86% and failed a gate it had no way of passing.

So the gate reads the worst single answer rather than the mean, and treats the
lexical score as what its own module says it is: a drift floor, not a quality
verdict. These tests pin that shape, because the temptation when a gate goes red
is to move the number.
"""

from __future__ import annotations

from askau.evaluation.runner import EvalReport, QuestionResult

THRESHOLDS = {"pass_rate": 0.90, "min_groundedness": 0.50}


def _answer(qid: str, groundedness: float, *, passed: bool = True) -> QuestionResult:
    return QuestionResult(
        question_id=qid,
        question="q",
        passed=passed,
        actual_state="grounded",
        groundedness=groundedness,
    )


def _report(*results: QuestionResult) -> EvalReport:
    report = EvalReport()
    report.results.extend(results)
    return report


class TestTheMeanCannotHideOneBadAnswer:
    def test_nineteen_good_and_one_adrift_fails(self) -> None:
        """The case the mean was blind to.

        Nineteen answers at 1.00 and one at 0.10 average to 95.5%, which sails
        past a 90% mean bar. But one answer wandered off its sources, and that is
        a person being told something the documents do not say.
        """
        results = [_answer(f"q{i}", 1.0) for i in range(19)] + [_answer("adrift", 0.10)]
        ok, breaches = _report(*results).gate(THRESHOLDS)
        assert not ok
        assert any("drift floor" in b for b in breaches), breaches

    def test_a_paraphrasing_model_passes(self) -> None:
        """What broke the old gate must not break this one.

        A real model spends sentences explaining which source it preferred, and
        those words are not in the evidence. Scores in the 0.6 to 0.8 range are
        normal for a model that writes rather than quotes, and are not drift.
        """
        results = [_answer("a", 0.67), _answer("b", 0.80), _answer("c", 1.0)]
        ok, breaches = _report(*results).gate(THRESHOLDS)
        assert ok, breaches

    def test_the_mean_is_reported_but_not_gated(self) -> None:
        report = _report(_answer("a", 0.60), _answer("b", 0.60))
        metrics = report.metrics()
        assert metrics["groundedness"] == 0.60
        assert metrics["min_groundedness"] == 0.60
        assert report.gate(THRESHOLDS)[0], "a 60% mean must not fail a 50% floor"


class TestSecurityIsAbsolute:
    def test_one_unauthorized_retrieval_fails_whatever_else_passes(self) -> None:
        """A build that leaks one document to one user is not 95% acceptable."""
        leak = QuestionResult(
            question_id="sec-001",
            question="q",
            passed=False,
            actual_state="grounded",
            failure="retrieved an unauthorized document",
            groundedness=1.0,
        )
        results = [_answer(f"q{i}", 1.0) for i in range(19)] + [leak]
        ok, breaches = _report(*results).gate(THRESHOLDS)
        assert not ok
        assert any("unauthorized" in b for b in breaches), breaches


class TestThresholdsAreNotQuietlyRelaxable:
    def test_the_shipped_thresholds_gate_the_worst_answer(self) -> None:
        """A regression guard on the gate's *shape*, not its value.

        Reverting to a mean threshold would make the gate green again and would
        also restore the blindness that let one adrift answer through. If the
        floor is ever removed, this fails and asks why.
        """
        from askau.evaluation.datasets import THRESHOLDS as SHIPPED

        assert "min_groundedness" in SHIPPED, (
            "the drift floor is gone — if the model-based judge now exists, gate "
            "on that instead and update this test to say so"
        )
        assert "groundedness" not in SHIPPED, (
            "a mean groundedness threshold is back; see the note in datasets.py "
            "for why the mean cannot be the gate"
        )
