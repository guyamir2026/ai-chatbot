"""
טסטים ל-buffer_after_event_minutes ב-google_calendar.get_available_slots
וב-bot.calendar_keyboard._calculate_day_slots.

הרעיון: אירוע ביומן 09:00-10:00 + buffer 15 ⇒ סלוטים שמתחילים לפני 10:15 חסומים.
"""

import sys
import types
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import patch, MagicMock

# ── Mock תלויות google שלא מותקנות בסביבת הטסטים ─────────────────────
# חייב להיות לפני import של google_calendar
for mod_name in (
    "google", "google.oauth2", "google.oauth2.credentials",
    "google_auth_oauthlib", "google_auth_oauthlib.flow",
    "googleapiclient", "googleapiclient.discovery", "googleapiclient.errors",
    "google.auth", "google.auth.transport", "google.auth.transport.requests",
):
    if mod_name not in sys.modules:
        sys.modules[mod_name] = types.ModuleType(mod_name)

# הצבת המחלקות/אובייקטים שגוגלים מצפים אליהם
sys.modules["google.oauth2.credentials"].Credentials = MagicMock()
sys.modules["google_auth_oauthlib.flow"].Flow = MagicMock()
sys.modules["googleapiclient.discovery"].build = MagicMock()


class _HttpError(Exception):
    pass


sys.modules["googleapiclient.errors"].HttpError = _HttpError
sys.modules["google.auth.transport.requests"].Request = MagicMock()

import pytest


IL_TZ = ZoneInfo("Asia/Jerusalem")


@pytest.fixture
def busy_event_9_to_10():
    """אירוע ביומן 09:00-10:00 בתאריך 2026-05-06."""
    return [{
        "start": "2026-05-06T09:00:00+03:00",
        "end": "2026-05-06T10:00:00+03:00",
    }]


def _patched_status(open_time="09:00", close_time="13:00"):
    return {
        "is_open": True,
        "open_time": open_time,
        "close_time": close_time,
        "reason": "",
        "source": "regular",
        "day_name": "רביעי",
    }


class TestGetAvailableSlotsBuffer:
    def test_no_buffer_slot_at_10_is_free(self, busy_event_9_to_10):
        from google_calendar import get_available_slots

        target = date(2026, 5, 6)
        with patch("google_calendar.get_busy_slots", return_value=busy_event_9_to_10), \
             patch("google_calendar._get_calendar_service", return_value=MagicMock()), \
             patch("business_hours.get_status_for_date", return_value=_patched_status()):
            slots = get_available_slots(
                target, service_duration_minutes=60,
                buffer_after_event_minutes=0,
            )
        # 10:00 צמוד לסיום האירוע ⇒ פנוי
        assert "10:00" in slots
        assert "09:00" not in slots
        assert "09:30" not in slots

    def test_buffer_15_blocks_10_00(self, busy_event_9_to_10):
        from google_calendar import get_available_slots

        target = date(2026, 5, 6)
        with patch("google_calendar.get_busy_slots", return_value=busy_event_9_to_10), \
             patch("google_calendar._get_calendar_service", return_value=MagicMock()), \
             patch("business_hours.get_status_for_date", return_value=_patched_status()):
            slots = get_available_slots(
                target, service_duration_minutes=60,
                buffer_after_event_minutes=15,
            )
        # האירוע נמשך 09:00-10:15 ⇒ סלוט 10:00 חסום
        assert "10:00" not in slots
        # סלוט 10:30 פנוי (אחרי ה-buffer)
        assert "10:30" in slots

    def test_buffer_30_blocks_10_30_too(self, busy_event_9_to_10):
        from google_calendar import get_available_slots

        target = date(2026, 5, 6)
        with patch("google_calendar.get_busy_slots", return_value=busy_event_9_to_10), \
             patch("google_calendar._get_calendar_service", return_value=MagicMock()), \
             patch("business_hours.get_status_for_date", return_value=_patched_status()):
            slots = get_available_slots(
                target, service_duration_minutes=60,
                buffer_after_event_minutes=30,
            )
        # האירוע נמשך 09:00-10:30 ⇒ 10:00 חסום, 10:30 חסום (סף תחתון == סוף אירוע מורחב — צמוד), 11:00 פנוי
        assert "10:00" not in slots
        assert "11:00" in slots

    def test_negative_buffer_treated_as_zero(self, busy_event_9_to_10):
        from google_calendar import get_available_slots

        target = date(2026, 5, 6)
        with patch("google_calendar.get_busy_slots", return_value=busy_event_9_to_10), \
             patch("google_calendar._get_calendar_service", return_value=MagicMock()), \
             patch("business_hours.get_status_for_date", return_value=_patched_status()):
            slots = get_available_slots(
                target, service_duration_minutes=60,
                buffer_after_event_minutes=-30,
            )
        # ערך שלילי ⇒ אפס. 10:00 פנוי כמו ב-no_buffer.
        assert "10:00" in slots


class TestCalendarKeyboardBuffer:
    def test_calculate_day_slots_buffer(self):
        from bot.calendar_keyboard import _calculate_day_slots

        target = date(2026, 5, 6)
        status = _patched_status()
        # אירוע 09:00-10:00 — busy_ranges במבנה של get_busy_slots
        month_busy = [{
            "start": "2026-05-06T09:00:00+03:00",
            "end": "2026-05-06T10:00:00+03:00",
        }]

        # פיקציה: now בעבר רחוק כדי שלא ייחתך מהבדיקה של "today"
        with patch("bot.calendar_keyboard.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2025, 1, 1, tzinfo=IL_TZ)
            # combine וסביבת datetime עדיין צריכים לעבוד
            mock_dt.combine = datetime.combine
            mock_dt.fromisoformat = datetime.fromisoformat

            slots_no_buf = _calculate_day_slots(
                target, status, month_busy, service_duration=60,
                buffer_after_event_minutes=0,
            )
            slots_with_buf = _calculate_day_slots(
                target, status, month_busy, service_duration=60,
                buffer_after_event_minutes=15,
            )

        assert "10:00" in slots_no_buf
        assert "10:00" not in slots_with_buf
        assert "10:30" in slots_with_buf
