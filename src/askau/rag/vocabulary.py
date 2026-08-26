"""Shared non-discriminating vocabulary.

Two checks depend on the same judgement — which words in a question actually
narrow it down. The clarification check asks whether the question has *any*
discriminating terms; the evidence gate asks whether those terms appear in what
was retrieved. Both give the wrong answer if organisational filler counts.

"What is the retirement age for Commission staff?" shares *commission* and
*staff* with every document in the corpus. Counting that as evidence lets an
unanswerable question pass the gate on vocabulary the corpus cannot help but
contain.

Kept in one module because the two checks drifting apart is a silent failure:
the gate would accept what the clarifier rejects, and neither would be wrong on
its own terms.
"""

from __future__ import annotations

#: Question-shaped words. Present in almost every query.
INTERROGATIVE = frozenset(
    {
        "what",
        "which",
        "when",
        "where",
        "who",
        "whom",
        "how",
        "why",
        "is",
        "are",
        "was",
        "were",
        "do",
        "does",
        "did",
        "can",
        "could",
        "should",
        "shall",
        "will",
        "would",
        "may",
        "might",
        "am",
        "be",
        "been",
    }
)

#: Grammatical scaffolding.
FUNCTION = frozenset(
    {
        "the",
        "a",
        "an",
        "of",
        "for",
        "and",
        "or",
        "to",
        "in",
        "on",
        "at",
        "as",
        "by",
        "with",
        "that",
        "this",
        "it",
        "from",
        "not",
        "any",
        "all",
        "there",
        "if",
        "then",
        "so",
        "but",
        "also",
        "same",
        "about",
        "into",
        "than",
        "i",
        "my",
        "me",
        "we",
        "our",
        "you",
        "your",
        "they",
        "their",
        "he",
        "she",
    }
)

#: Institutional vocabulary. Every approved AUC document contains most of these,
#: so matching one tells you nothing about whether the right document was found.
ORGANISATIONAL = frozenset(
    {
        "auc",
        "au",
        "commission",
        "union",
        "african",
        "africa",
        "staff",
        "member",
        "members",
        "employee",
        "employees",
        "personnel",
        "policy",
        "policies",
        "procedure",
        "procedures",
        "rule",
        "rules",
        "document",
        "documents",
        "guideline",
        "guidelines",
        "directive",
        "circular",
        "regulation",
        "regulations",
        "provision",
        "provisions",
        "shall",
        "must",
        "required",
        "applicable",
        "relevant",
        "tell",
        "explain",
        "describe",
        "know",
        "need",
        "want",
    }
)

#: Everything that carries no discriminating power on its own.
NON_DISCRIMINATING = INTERROGATIVE | FUNCTION | ORGANISATIONAL


def discriminating_words(text: str, *, min_length: int = 3) -> set[str]:
    """The words in ``text`` that actually narrow down which document is meant."""
    import re

    return {
        w.lower()
        for w in re.findall(r"\w+", text, re.UNICODE)
        if w.lower() not in NON_DISCRIMINATING and len(w) >= min_length
    }
