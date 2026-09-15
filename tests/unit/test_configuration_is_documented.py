"""Every setting is in `.env.example` and in `docs/configuration.md`.

Infrastructure lives outside this repository, so a missing or misunderstood
environment variable is the most likely first-run failure. `Settings` validates
aggressively for that reason — but validation only helps somebody who knows the
variable exists.

A setting added to `settings.py` and nowhere else is invisible: it has a default,
so nothing fails, and the person who needed to change it never learns they could.
That is how `ASKAU_LLM_*` came to be absent from `.env.example` for months while
being fully implemented, and how four answer-quality thresholds — the ones that
decide when AskAU refuses to answer — stayed undocumented.

Checked mechanically because prose drifts and nobody notices.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SETTING = re.compile(r"^    ([a-z][a-z0-9_]*)\s*:", re.M)
_ENV_VAR = re.compile(r"^(ASKAU_[A-Z0-9_]+)=", re.M)


def _declared() -> set[str]:
    source = (_ROOT / "src/askau/settings.py").read_text()
    # Only the Settings class body; helper dataclasses below it are not config.
    body = source.split("class Settings(BaseSettings):", 1)[1].split("\n    @", 1)[0]
    return {"ASKAU_" + name.upper() for name in _SETTING.findall(body)}


class TestConfigurationIsDiscoverable:
    def test_every_setting_appears_in_env_example(self) -> None:
        declared = _declared()
        present = set(_ENV_VAR.findall((_ROOT / ".env.example").read_text()))
        missing = sorted(declared - present)
        assert not missing, (
            f"settings absent from .env.example: {missing}. They have defaults, so "
            "nothing fails — which is the problem: whoever needed to change one "
            "will never learn it exists."
        )

    def test_every_setting_appears_in_the_reference(self) -> None:
        reference = (_ROOT / "docs/configuration.md").read_text()
        missing = sorted(name for name in _declared() if name not in reference)
        assert not missing, f"settings absent from docs/configuration.md: {missing}"

    def test_the_example_invents_nothing(self) -> None:
        """A variable in `.env.example` that no setting reads is worse than an
        absent one: somebody will set it and expect an effect."""
        declared = _declared()
        present = set(_ENV_VAR.findall((_ROOT / ".env.example").read_text()))
        unknown = sorted(present - declared)
        assert not unknown, f".env.example names variables nothing reads: {unknown}"
