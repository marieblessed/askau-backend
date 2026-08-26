"""Language detection — ADR-0015. The failure mode here is silent."""

from __future__ import annotations

from askau.ingestion.language import detect_language

EN = (
    "The Commission shall reimburse economy class airfare for all official travel "
    "undertaken by staff members and approved in advance by the directorate."
)
FR = (
    "La Commission doit rembourser les billets en classe économique pour tous les "
    "voyages officiels effectués par les membres du personnel et approuvés."
)
AR = "تلتزم المفوضية بسداد تكاليف تذاكر السفر بالدرجة الاقتصادية لجميع الرحلات الرسمية."
AM = "ኮሚሽኑ ለሁሉም ኦፊሴላዊ ጉዞዎች የኢኮኖሚ ክፍል የአውሮፕላን ትኬት ወጪን ይመልሳል። ሠራተኞች በቅድሚያ ማጽደቅ አለባቸው።"


class TestDeclaredLanguageWins:
    def test_metadata_beats_detection(self) -> None:
        """The repository owner knows better than a heuristic."""
        assert detect_language(EN, declared="fr").language == "fr"

    def test_declared_region_is_stripped(self) -> None:
        assert detect_language(EN, declared="en-GB").language == "en"

    def test_declared_unknown_language_is_not_confident(self) -> None:
        assert not detect_language(EN, declared="am").confident


class TestScriptDetection:
    def test_arabic(self) -> None:
        g = detect_language(AR)
        assert g.language == "ar"
        assert g.ts_config == "arabic"

    def test_ethiopic_detected_but_falls_back_to_simple(self) -> None:
        """Amharic is identified, yet Postgres ships no Amharic stemmer — so the
        text-search config must be `simple`, not a guess."""
        g = detect_language(AM)
        assert g.language == "am"
        assert g.ts_config == "simple"


class TestLatinScript:
    def test_english(self) -> None:
        assert detect_language(EN).ts_config == "english"

    def test_french(self) -> None:
        assert detect_language(FR).ts_config == "french"


class TestConservativeFallback:
    def test_too_short_to_judge(self) -> None:
        g = detect_language("Leave policy.")
        assert not g.confident
        assert g.ts_config == "simple"

    def test_empty(self) -> None:
        assert detect_language("").ts_config == "simple"

    def test_unconfident_never_yields_english(self) -> None:
        """The defect ADR-0015 exists to prevent: defaulting everything to English."""
        assert detect_language("###   ---   ***").ts_config == "simple"
