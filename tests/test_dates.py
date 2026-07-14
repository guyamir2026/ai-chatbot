"""טסטים ל-utils/dates.py — פורמט תאריך ישראלי (DD/MM/YYYY)."""

from __future__ import annotations

from utils.dates import format_il_date


class TestFormatIlDate:
    def test_iso_to_israeli(self):
        assert format_il_date("2026-07-18") == "18/07/2026"
        assert format_il_date("2026-04-01") == "01/04/2026"
        assert format_il_date("2026-12-31") == "31/12/2026"

    def test_empty_returns_empty(self):
        # ריק נשאר ריק — כדי שבדיקות `if end_date:` יישארו תקינות.
        assert format_il_date("") == ""

    def test_none_returns_empty(self):
        assert format_il_date(None) == ""

    def test_whitespace_trimmed(self):
        assert format_il_date("  2026-07-18  ") == "18/07/2026"

    def test_malformed_returned_as_is(self):
        # פורמט לא צפוי — fail-safe, מוחזר כמו שהוא (לא זורק).
        assert format_il_date("18/07/2026") == "18/07/2026"
        assert format_il_date("not-a-date") == "not-a-date"
        assert format_il_date("2026-13-99") == "2026-13-99"
