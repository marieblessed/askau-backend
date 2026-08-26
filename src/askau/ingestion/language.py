"""Language detection driving the per-document text-search configuration.

ADR-0015: the keyword arm must stem each document in its own language. Detection
is deliberately conservative — an unconfident guess resolves to ``simple``
(tokenize, do not stem), because mis-stemming produces wrong tokens silently
whereas not stemming merely gives up recall we can measure.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from askau.domain.enums import TS_CONFIG_BY_LANGUAGE, ts_config_for

#: Script ranges that identify a language without needing a statistical model.
#: Ethiopic matters institutionally: the Commission is headquartered in Addis Ababa.
# Written as escapes rather than literals: the literal forms are visually
# ambiguous with Latin characters, which is exactly how a script range silently
# stops matching after an innocent-looking edit.
_SCRIPTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("am", re.compile(r"[\u1200-\u137f]")),  # Ethiopic
    ("ar", re.compile(r"[\u0600-\u06ff]")),  # Arabic
    ("ru", re.compile(r"[\u0400-\u04ff]")),  # Cyrillic
)

_log = logging.getLogger(__name__)

_MIN_CHARS = 40
_SCRIPT_SHARE = 0.15


@dataclass(frozen=True, slots=True)
class LanguageGuess:
    language: str
    confident: bool

    @property
    def ts_config(self) -> str:
        """Unconfident detections fall back to ``simple``."""
        return ts_config_for(self.language) if self.confident else "simple"


def detect_language(text: str, *, declared: str | None = None) -> LanguageGuess:
    """Resolve a document's language.

    A declared language from source metadata beats detection: the repository
    owner knows better than a heuristic, and FR-016 already requires that
    metadata be carried.
    """
    if declared:
        code = declared.split("-")[0].lower()
        return LanguageGuess(code, confident=code in TS_CONFIG_BY_LANGUAGE)

    sample = text.strip()
    if len(sample) < _MIN_CHARS:
        # Too little evidence. Refusing to guess is the safe failure here.
        return LanguageGuess("und", confident=False)

    window = sample[:4000]
    for code, pattern in _SCRIPTS:
        if len(pattern.findall(window)) / len(window) >= _SCRIPT_SHARE:
            # Amharic has no Postgres stemmer, so `confident` is about identifying
            # the language, not about whether a stemmer exists — ts_config_for
            # handles that separately.
            return LanguageGuess(code, confident=True)

    return _detect_latin(window)


def _detect_latin(text: str) -> LanguageGuess:
    """Distinguish the Latin-script AU languages by function-word frequency.

    A statistical model would be better; this avoids a dependency for a decision
    whose wrong answer is "fall back to simple". Where `langdetect` is installed
    it is preferred.
    """
    try:
        from langdetect import DetectorFactory, detect

        DetectorFactory.seed = 0  # deterministic — ingestion must be reproducible
        code = str(detect(text)).split("-")[0].lower()
        return LanguageGuess(code, confident=code in TS_CONFIG_BY_LANGUAGE)
    except ImportError:
        _log.debug("langdetect not installed; using the function-word heuristic")
    except Exception as exc:
        # Broad by intent: a detection failure must degrade to `simple`, never
        # abort ingestion of an otherwise valid document.
        _log.warning("language detection failed, falling back to heuristic: %s", exc)

    markers: dict[str, tuple[str, ...]] = {
        "en": (" the ", " and ", " shall ", " of the ", " for "),
        "fr": (" le ", " la ", " les ", " des ", " doit ", " pour "),
        "pt": (" o ", " a ", " os ", " das ", " deve ", " para "),
        "es": (" el ", " la ", " los ", " del ", " debe ", " para "),
        "sw": (" na ", " ya ", " wa ", " kwa ", " katika "),
    }
    lowered = f" {text.lower()} "
    scores = {code: sum(lowered.count(m) for m in words) for code, words in markers.items()}
    best = max(scores, key=lambda k: scores[k])
    return LanguageGuess(best, confident=scores[best] >= 3)
