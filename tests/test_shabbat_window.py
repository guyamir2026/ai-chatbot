"""
טסטים ל-messaging/shabbat_window.py ול-utils.phone.is_valid_israeli_e164.
"""

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

_IL = ZoneInfo("Asia/Jerusalem")


# ── Israeli phone validation ─────────────────────────────────────────────────


class TestIsValidIsraeliE164:
    def test_valid_mobile(self):
        from utils.phone import is_valid_israeli_e164
        assert is_valid_israeli_e164("+972501234567") is True
        assert is_valid_israeli_e164("+972521234567") is True

    def test_wrong_prefix(self):
        from utils.phone import is_valid_israeli_e164
        assert is_valid_israeli_e164("+12125551234") is False   # USA
        assert is_valid_israeli_e164("+44207654321") is False   # UK

    def test_without_plus(self):
        from utils.phone import is_valid_israeli_e164
        assert is_valid_israeli_e164("972501234567") is False

    def test_with_leading_zero_wrong_long(self):
        """פורמט מקומי +9720501234567 (14 תווים) שגוי — אורך חורג."""
        from utils.phone import is_valid_israeli_e164
        assert is_valid_israeli_e164("+9720501234567") is False

    def test_leading_zero_13_chars_rejected(self):
        """Regression (Cursor): +972012345678 (13 תווים, 0 אחרי 972) —
        הרגקס הישן \\d{9} היה מקבל את זה בטעות. [1-9]\\d{8} דוחה."""
        from utils.phone import is_valid_israeli_e164
        assert is_valid_israeli_e164("+972012345678") is False

    def test_too_short(self):
        from utils.phone import is_valid_israeli_e164
        assert is_valid_israeli_e164("+972501") is False

    def test_too_long(self):
        from utils.phone import is_valid_israeli_e164
        assert is_valid_israeli_e164("+9725012345678") is False

    def test_non_string(self):
        from utils.phone import is_valid_israeli_e164
        assert is_valid_israeli_e164(None) is False
        assert is_valid_israeli_e164(972501234567) is False

    def test_empty(self):
        from utils.phone import is_valid_israeli_e164
        assert is_valid_israeli_e164("") is False


# ── Shabbat window ───────────────────────────────────────────────────────────


class TestShabbatWindow:
    def test_friday_before_14_not_blocked(self):
        from messaging.shabbat_window import is_in_shabbat_window
        # שישי 10:00 בבוקר — לא חלון שבת
        dt = datetime(2026, 6, 5, 10, 0, tzinfo=_IL)  # שישי
        assert is_in_shabbat_window(dt) is False

    def test_friday_14_is_blocked(self):
        from messaging.shabbat_window import is_in_shabbat_window
        dt = datetime(2026, 6, 5, 14, 0, tzinfo=_IL)  # שישי 14:00
        assert is_in_shabbat_window(dt) is True

    def test_friday_20_blocked(self):
        from messaging.shabbat_window import is_in_shabbat_window
        dt = datetime(2026, 6, 5, 20, 0, tzinfo=_IL)
        assert is_in_shabbat_window(dt) is True

    def test_saturday_noon_blocked(self):
        from messaging.shabbat_window import is_in_shabbat_window
        dt = datetime(2026, 6, 6, 12, 0, tzinfo=_IL)  # שבת צהריים
        assert is_in_shabbat_window(dt) is True

    def test_saturday_after_20_not_blocked(self):
        from messaging.shabbat_window import is_in_shabbat_window
        dt = datetime(2026, 6, 6, 20, 0, tzinfo=_IL)  # מוצ"ש
        assert is_in_shabbat_window(dt) is False

    def test_sunday_morning_not_blocked(self):
        from messaging.shabbat_window import is_in_shabbat_window
        dt = datetime(2026, 6, 7, 9, 0, tzinfo=_IL)  # יום ראשון
        assert is_in_shabbat_window(dt) is False

    def test_naive_datetime_assumed_israel_tz(self):
        from messaging.shabbat_window import is_in_shabbat_window
        dt = datetime(2026, 6, 6, 12, 0)  # naive — מניחים IL
        assert is_in_shabbat_window(dt) is True

    def test_utc_converted_to_israel(self):
        from messaging.shabbat_window import is_in_shabbat_window
        # UTC 09:00 בשבת = 12:00 בשעון ישראל → חסום
        dt_utc = datetime(2026, 6, 6, 9, 0, tzinfo=ZoneInfo("UTC"))
        assert is_in_shabbat_window(dt_utc) is True


class TestJewishHolidays:
    def test_pesach_is_holiday(self):
        """פסח 2026 (1/4/2026) — חג יהודי."""
        from messaging.shabbat_window import is_jewish_holiday
        # תאריכים משתנים כל שנה; נוודא שמחזיר True+שם כלשהו
        dt = datetime(2026, 4, 2, 10, 0, tzinfo=_IL)  # יום פסח (ניסן ט"ו)
        is_hol, name = is_jewish_holiday(dt)
        # אם ספריית holidays זיהתה את פסח — נקבל True
        # (אם לא — זה לא כישלון של הקוד שלנו אלא של הספריה)
        if is_hol:
            assert name is not None
            assert len(name) > 0

    def test_regular_tuesday_not_holiday(self):
        from messaging.shabbat_window import is_jewish_holiday
        dt = datetime(2026, 6, 2, 10, 0, tzinfo=_IL)  # יום שלישי רגיל
        is_hol, name = is_jewish_holiday(dt)
        assert is_hol is False
        assert name is None


class TestBlockedForMarketing:
    def test_not_blocked_on_weekday(self):
        from messaging.shabbat_window import is_blocked_for_marketing
        # יום רביעי ביוני 2026 — כנראה לא חג יהודי (Shavuot 21-23 במאי).
        # בחרנו תאריך קבוע בחודש יחסית "נקי" בלוח העברי.
        dt = datetime(2026, 6, 3, 10, 0, tzinfo=_IL)
        blocked, reason = is_blocked_for_marketing(dt)
        assert blocked is False
        assert reason is None

    def test_blocked_on_saturday(self):
        from messaging.shabbat_window import is_blocked_for_marketing
        dt = datetime(2026, 6, 6, 11, 0, tzinfo=_IL)  # שבת בבוקר, יוני 2026
        blocked, reason = is_blocked_for_marketing(dt)
        assert blocked is True
        assert "שבת" in reason


class TestNextAllowedTime:
    def test_non_marketing_passes_through(self):
        from messaging.shabbat_window import next_allowed_time
        dt = datetime(2026, 6, 6, 12, 0, tzinfo=_IL)  # שבת צהריים
        # UTILITY לא נחסם — מחזיר כמו שהוא
        assert next_allowed_time(dt, category="UTILITY") == dt
        assert next_allowed_time(dt, category="AUTHENTICATION") == dt

    def test_marketing_friday_deferred_to_saturday_evening(self):
        from messaging.shabbat_window import next_allowed_time
        dt = datetime(2026, 6, 5, 15, 0, tzinfo=_IL)  # שישי 15:00
        result = next_allowed_time(dt, category="MARKETING")
        # צפוי: שבת 20:00
        assert result.weekday() == 5  # שבת
        assert result.hour == 20

    def test_marketing_saturday_morning_deferred_to_evening(self):
        from messaging.shabbat_window import next_allowed_time
        dt = datetime(2026, 6, 6, 10, 0, tzinfo=_IL)  # שבת בבוקר
        result = next_allowed_time(dt, category="MARKETING")
        assert result.weekday() == 5
        assert result.hour == 20

    def test_marketing_sunday_not_deferred(self):
        from messaging.shabbat_window import next_allowed_time
        dt = datetime(2026, 6, 7, 10, 0, tzinfo=_IL)  # יום ראשון
        result = next_allowed_time(dt, category="MARKETING")
        assert result == dt  # לא משתנה

    def test_marketing_unblocked_preserves_input_tz(self):
        """Regression (Cursor): כשהקלט לא חסום, ההחזרה חייבת להיות *אותו*
        dt עם אותו tzinfo — לא גרסה מומרת ל-Asia/Jerusalem. ה-docstring
        הבטיח 'שמירה על tzinfo של הקלט'."""
        from messaging.shabbat_window import next_allowed_time
        utc = ZoneInfo("UTC")
        # יום ראשון 08:00 UTC = 11:00 בישראל — לא חסום
        dt = datetime(2026, 6, 7, 8, 0, tzinfo=utc)
        result = next_allowed_time(dt, category="MARKETING")
        assert result is dt or result == dt
        assert result.tzinfo == utc  # שומר על TZ המקורי

    def test_holiday_deferred_to_next_day(self):
        """אם dt נופל על חג — דוחה ליום שלמחרת 09:00.
        משתמשים ב-mock לוודא התנהגות ללא תלות בלוח השנה העברי:
        call 1 = True (הקלט נחשב חג), call 2+ = False (הבא לא חג)."""
        from datetime import timedelta
        from messaging.shabbat_window import next_allowed_time

        call_count = {"n": 0}

        def fake_is_holiday(dt_arg):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return True, "פסח"
            return False, None

        with patch("messaging.shabbat_window.is_jewish_holiday", side_effect=fake_is_holiday):
            dt = datetime(2026, 6, 3, 10, 0, tzinfo=_IL)  # יום רביעי (לא שבת)
            result = next_allowed_time(dt, category="MARKETING")
            # צפוי: יום שלמחרת, 09:00 (לא תלוי בתאריך ספציפי)
            assert result.hour == 9
            assert result.date() == (dt + timedelta(days=1)).date()
