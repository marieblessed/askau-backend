"""Follow-up resolution (FR-004).

"Does this apply to staff on probation?" is meaningless on its own. It inherits
its subject from the previous turn, and retrieval has no way to know that.

The resolution is deliberately narrow: **carry the subject, not the wording.**
Concatenating the previous question onto the current one is the obvious approach
and it is wrong — every keyword from the earlier turn then competes in the
search, so a follow-up about probation retrieves the travel policy because the
last question mentioned travel. What carries forward is the noun phrase the
follow-up is pointing at, and nothing else.

A follow-up is only rewritten when it is *unresolvable alone*: short, and opening
with a referring expression. A self-contained question is left exactly as asked,
because rewriting one that did not need it is how a working search turns into a
mysterious one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_WORD = re.compile(r"[\w'-]+", re.UNICODE)

#: Openers that point at something already said rather than naming it.
_REFERRING = (
    "does this",
    "does that",
    "is this",
    "is that",
    "what about",
    "how about",
    "and for",
    "what if",
    "does it",
    "is it",
    "do they",
    "does he",
    "does she",
    "can i",
    "can they",
    "what are they",
    "who does",
    "when does",
    "why does",
)

#: Words that never carry the subject of a question.
_STOP = frozenset(
    {
        "what",
        "which",
        "when",
        "where",
        "who",
        "how",
        "why",
        "is",
        "are",
        "was",
        "were",
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
        "i",
        "my",
        "we",
        "our",
        "you",
        "your",
        "they",
        "their",
        "about",
        "apply",
        "applies",
        "there",
        "any",
        "all",
        "also",
        "same",
        "if",
        "then",
        "so",
        "but",
    }
)

#: A follow-up is short. A long question carries its own subject even if it
#: happens to open with "what about".
_MAX_FOLLOWUP_WORDS = 12


@dataclass(frozen=True, slots=True)
class Resolution:
    """The query to retrieve with, and whether it was rewritten."""

    query: str
    rewritten: bool
    inherited_subject: str = ""


def resolve(question: str, history: list[tuple[str, str]] | None) -> Resolution:
    """Rewrite a referring follow-up into a standalone retrieval query."""
    if not history:
        return Resolution(question, rewritten=False)

    stripped = question.strip()
    words = _WORD.findall(stripped)
    if len(words) > _MAX_FOLLOWUP_WORDS:
        return Resolution(question, rewritten=False)

    lowered = stripped.lower()
    if not any(lowered.startswith(opener) for opener in _REFERRING):
        return Resolution(question, rewritten=False)

    subject = _subject_of(history)
    if not subject:
        return Resolution(question, rewritten=False)

    # Subject first: the terms that identify the document lead, and the
    # follow-up's own qualifier narrows within it.
    return Resolution(f"{subject} {stripped}", rewritten=True, inherited_subject=subject)


def _subject_of(history: list[tuple[str, str]], max_terms: int = 4) -> str:
    """Content terms from the most recent user question.

    Only the question, never the answer: an answer is long, and its incidental
    vocabulary would swamp the follow-up it is meant to support.
    """
    for role, content in reversed(history):
        if role != "user":
            continue
        terms: list[str] = []
        for word in _WORD.findall(content.lower()):
            if word in _STOP or len(word) <= 2 or word in terms:
                continue
            terms.append(word)
            if len(terms) >= max_terms:
                break
        return " ".join(terms)
    return ""
