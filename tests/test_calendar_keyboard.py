"""
טסטים למודול bot/calendar_keyboard — לוח שנה ויזואלי inline.
"""

import sys
import types
from datetime import date, timedelta
from unittest.mock import patch, MagicMock, call

import pytest


# ── פירוק callback ──────────────────────────────────────────────────────────


class TestParseCalendarCallback:
    """טסטים לפירוק callback_data של לוח השנה."""

    def test_parse_select(self):
        from bot.calendar_keyboard import parse_calendar_callback, CB_CALENDAR_SELECT

        result = parse_calendar_callback(f"{CB_CALENDAR_SELECT}2026-04-15")
        assert result["action"] == "select"
        assert result["date"] == "2026-04-15"

    def test_parse_prev(self):
        from bot.calendar_keyboard import parse_calendar_callback, CB_CALENDAR_PREV

        result = parse_calendar_callback(f"{CB_CALENDAR_PREV}2026_3")
        assert result["action"] == "prev"
        assert result["year"] == 2026
        assert result["month"] == 3

    def test_parse_next(self):
        from bot.calendar_keyboard import parse_calendar_callback, CB_CALENDAR_NEXT

        result = parse_calendar_callback(f"{CB_CALENDAR_NEXT}2026_5")
        assert result["action"] == "next"
        assert result["year"] == 2026
        assert result["month"] == 5

    def test_parse_ignore(self):
        from bot.calendar_keyboard import parse_calendar_callback, CB_CALENDAR_IGNORE

        result = parse_calendar_callback(CB_CALENDAR_IGNORE)
        assert result["action"] == "ignore"

    def test_parse_unknown(self):
        from bot.calendar_keyboard import parse_calendar_callback

        result = parse_calendar_callback("unknown_data")
        assert result["action"] == "ignore"


# ── זמינות חודש ─────────────────────────────────────────────────────────────


class TestGetMonthAvailability:
    """טסטים ל-get_month_availability."""

    @patch("bot.calendar_keyboard.VacationService")
    @patch("bot.calendar_keyboard.get_status_for_date")
    def test_past_days_unavailable(self, mock_status, mock_vacation):
        """ימים שעברו מסומנים כלא זמינים."""
        from bot.calendar_keyboard import get_month_availability

        mock_vacation.is_active.return_value = False
        mock_status.return_value = {"is_open": True, "open_time": "09:00", "close_time": "18:00"}

        today = date.today()
        if today.day > 1:
            result = get_month_availability(today.year, today.month)
            assert result[1]["available"] is False
            assert result[1]["reason"] == "עבר"

    @patch("bot.calendar_keyboard.VacationService")
    @patch("bot.calendar_keyboard.get_status_for_date")
    def test_vacation_blocks_all_days(self, mock_status, mock_vacation):
        """חופשה פעילה חוסמת את כל הימים."""
        from bot.calendar_keyboard import get_month_availability

        mock_vacation.is_active.return_value = True

        # חודש עתידי כדי שלא יהיו ימים שעברו
        future = date.today() + timedelta(days=60)
        result = get_month_availability(future.year, future.month)

        for day_info in result.values():
            assert day_info["available"] is False
            assert day_info["reason"] == "חופשה"

    @patch("bot.calendar_keyboard.VacationService")
    @patch("bot.calendar_keyboard.get_status_for_date")
    def test_closed_day_unavailable(self, mock_status, mock_vacation):
        """יום סגור (לפי business_hours) מסומן כלא זמין."""
        from bot.calendar_keyboard import get_month_availability

        mock_vacation.is_active.return_value = False
        mock_status.return_value = {"is_open": False, "reason": "שבת"}

        future = date.today() + timedelta(days=60)
        result = get_month_availability(future.year, future.month)

        # רק ימים עתידיים — כולם סגורים
        future_days = {d: info for d, info in result.items() if info["reason"] != "עבר"}
        for day_info in future_days.values():
            assert day_info["available"] is False

    @patch("bot.calendar_keyboard.VacationService")
    @patch("bot.calendar_keyboard.get_status_for_date")
    def test_open_day_available_without_gcal(self, mock_status, mock_vacation):
        """יום פתוח ללא Google Calendar — זמין."""
        from bot.calendar_keyboard import get_month_availability

        mock_vacation.is_active.return_value = False
        mock_status.return_value = {
            "is_open": True, "open_time": "09:00", "close_time": "18:00",
        }

        future = date.today() + timedelta(days=60)
        result = get_month_availability(future.year, future.month)

        # לפחות יום אחד אמור להיות זמין
        available_days = [d for d, info in result.items() if info["available"]]
        assert len(available_days) > 0


# ── חישוב slots ליום ────────────────────────────────────────────────────────


class TestCalculateDaySlots:
    """טסטים ל-_calculate_day_slots."""

    def test_no_busy_ranges_returns_all_slots(self):
        """יום ללא תפוסות — כל ה-slots פנויים."""
        from bot.calendar_keyboard import _calculate_day_slots

        future = date.today() + timedelta(days=30)
        status = {"open_time": "09:00", "close_time": "12:00"}

        slots = _calculate_day_slots(future, status, [], 60)
        # 09:00, 09:30, 10:00, 10:30, 11:00 — 5 slots (60 דקות כל אחד, מ-09:00 עד 12:00)
        assert "09:00" in slots
        assert "11:00" in slots
        # 11:30 + 60 דקות = 12:30 > 12:00, אז 11:30 לא נכנס
        assert "11:30" not in slots

    def test_fully_busy_returns_empty(self):
        """יום תפוס לחלוטין — ללא slots."""
        from bot.calendar_keyboard import _calculate_day_slots
        from datetime import datetime, time as _time
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("Asia/Jerusalem")
        future = date.today() + timedelta(days=30)
        status = {"open_time": "09:00", "close_time": "12:00"}

        busy = [{
            "start": datetime.combine(future, _time(9, 0), tzinfo=tz).isoformat(),
            "end": datetime.combine(future, _time(12, 0), tzinfo=tz).isoformat(),
        }]

        slots = _calculate_day_slots(future, status, busy, 60)
        assert slots == []

    def test_partial_busy_returns_free_slots(self):
        """יום חלקית תפוס — מחזיר רק slots פנויים."""
        from bot.calendar_keyboard import _calculate_day_slots
        from datetime import datetime, time as _time
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("Asia/Jerusalem")
        future = date.today() + timedelta(days=30)
        status = {"open_time": "09:00", "close_time": "14:00"}

        # תפוס 09:00-11:00
        busy = [{
            "start": datetime.combine(future, _time(9, 0), tzinfo=tz).isoformat(),
            "end": datetime.combine(future, _time(11, 0), tzinfo=tz).isoformat(),
        }]

        slots = _calculate_day_slots(future, status, busy, 60)
        assert "09:00" not in slots
        assert "10:00" not in slots
        assert "11:00" in slots
        assert "12:00" in slots

    def test_short_service_more_slots(self):
        """שירות קצר (30 דקות) — יותר slots זמינים."""
        from bot.calendar_keyboard import _calculate_day_slots

        future = date.today() + timedelta(days=30)
        status = {"open_time": "09:00", "close_time": "12:00"}

        slots = _calculate_day_slots(future, status, [], 30)
        # 30 דקות: 09:00, 09:30, 10:00, 10:30, 11:00, 11:30 — 6 slots
        assert len(slots) == 6
        assert "11:30" in slots
