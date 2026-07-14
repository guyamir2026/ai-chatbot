"""
טסטים לטבלת התרחישים של core/booking_decision.

כל מקרה בטבלה (ראה תיעוד PR auto-calendar-booking) מקבל לפחות בדיקה אחת.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from core.booking_decision import (
    BookingDecisionInput,
    decide_appointment_status,
    get_rejection_message,
)


IL_TZ = ZoneInfo("Asia/Jerusalem")
NOW = datetime(2026, 5, 5, 9, 0, tzinfo=IL_TZ)  # יום שלישי, 9:00


def _open_status(open_time: str = "09:00", close_time: str = "18:00") -> dict:
    """Helper — סטטוס יום עסקים פתוח."""
    return {
        "is_open": True,
        "open_time": open_time,
        "close_time": close_time,
        "reason": "",
        "source": "regular",
        "day_name": "שלישי",
    }


def _closed_status(source: str = "regular", reason: str = "סגור") -> dict:
    return {
        "is_open": False,
        "open_time": None,
        "close_time": None,
        "reason": reason,
        "source": source,
        "day_name": "שבת",
    }


def _input(**overrides) -> BookingDecisionInput:
    """ברירת מחדל סבירה — יום מחר ב-10:00, 60 דקות, יום פתוח, אין קונפליקטים, GCal פנוי."""
    defaults = dict(
        mode="auto_with_check",
        slot_date=NOW.date() + timedelta(days=1),
        slot_time=time(10, 0),
        duration_minutes=60,
        now_il=NOW,
        max_days_ahead=90,
        business_hours_status=_open_status(),
        vacation_active=False,
        has_pending_or_confirmed_conflict=False,
        user_has_appointment_same_day=False,
        calendar_connected=True,
        calendar_check_failed=False,
        available_slots=["09:00", "10:00", "11:00", "12:00"],
    )
    defaults.update(overrides)
    return BookingDecisionInput(**defaults)


# ─── #1 סלוט בעבר ────────────────────────────────────────────────────────
class TestSlotInPast:
    def test_past_slot_rejected_in_all_modes(self):
        for mode in ("manual", "auto_with_check", "auto_always"):
            inp = _input(
                mode=mode,
                slot_date=NOW.date(),
                slot_time=time(8, 0),  # שעה לפני ה-NOW=9:00
            )
            res = decide_appointment_status(inp)
            assert res.decision == "rejected", f"mode={mode}"
            assert res.reason == "slot_in_past"


# ─── #2 סלוט רחוק מאוד ──────────────────────────────────────────────────
class TestSlotTooFarAhead:
    def test_far_future_rejected(self):
        for mode in ("manual", "auto_with_check", "auto_always"):
            inp = _input(mode=mode, slot_date=NOW.date() + timedelta(days=200))
            res = decide_appointment_status(inp)
            assert res.decision == "rejected", f"mode={mode}"
            assert res.reason == "slot_too_far_ahead"

    def test_within_window_ok(self):
        inp = _input(slot_date=NOW.date() + timedelta(days=89))
        res = decide_appointment_status(inp)
        assert res.decision == "confirmed"


# ─── #3 סלוט תקין בשעות עבודה ──────────────────────────────────────────
class TestRegularSlot:
    def test_manual_returns_pending(self):
        res = decide_appointment_status(_input(mode="manual"))
        assert res.decision == "pending"
        assert res.reason == "manual_mode"

    def test_auto_with_check_returns_confirmed(self):
        res = decide_appointment_status(_input(mode="auto_with_check"))
        assert res.decision == "confirmed"
        assert res.reason == "auto_with_check_ok"

    def test_auto_always_returns_confirmed(self):
        res = decide_appointment_status(_input(mode="auto_always"))
        assert res.decision == "confirmed"


# ─── #4 שבת ──────────────────────────────────────────────────────────────
class TestShabbat:
    def test_manual_still_pending(self):
        # ב-manual גם בשבת — בעל עסק יחליט (יכול לעבוד בשבת חריגה)
        inp = _input(mode="manual", business_hours_status=_closed_status("regular", "שבת"))
        res = decide_appointment_status(inp)
        assert res.decision == "pending"

    def test_auto_with_check_rejects(self):
        inp = _input(business_hours_status=_closed_status("regular", "שבת"))
        res = decide_appointment_status(inp)
        assert res.decision == "rejected"
        assert res.reason == "closed_regular"

    def test_auto_always_does_NOT_override_vacation(self):
        # שבת ב-auto_always — ללא vacation, מאשר. המשתמש ביקש.
        inp = _input(mode="auto_always", business_hours_status=_closed_status("regular", "שבת"))
        res = decide_appointment_status(inp)
        assert res.decision == "confirmed"


# ─── #5 חג ───────────────────────────────────────────────────────────────
class TestHoliday:
    def test_auto_with_check_rejects_holiday(self):
        inp = _input(business_hours_status=_closed_status("holiday", "פסח"))
        res = decide_appointment_status(inp)
        assert res.decision == "rejected"
        assert res.reason == "closed_holiday"


# ─── #6 חג שסומן כפעיל (user_removed) — מגיע כ-is_open=True ────────────
class TestHolidayWorked:
    def test_auto_with_check_confirms(self):
        # business_hours.get_status_for_date ידחה את החג ויחזיר "regular" עם is_open
        res = decide_appointment_status(_input())
        assert res.decision == "confirmed"


# ─── #7 special_day סגור ────────────────────────────────────────────────
class TestSpecialDayClosed:
    def test_rejected(self):
        inp = _input(business_hours_status=_closed_status("special_day", "חופש"))
        res = decide_appointment_status(inp)
        assert res.decision == "rejected"
        assert res.reason == "closed_special_day"


# ─── #8 vacation ─────────────────────────────────────────────────────────
class TestVacation:
    def test_auto_with_check_rejects(self):
        res = decide_appointment_status(_input(vacation_active=True))
        assert res.decision == "rejected"
        assert res.reason == "vacation_active"

    def test_auto_always_rejects_vacation(self):
        # vacation גובר על auto_always — בעל עסק הפעיל את זה ידנית
        res = decide_appointment_status(_input(mode="auto_always", vacation_active=True))
        assert res.decision == "rejected"
        assert res.reason == "vacation_active"

    def test_manual_ignores_vacation(self):
        # ב-manual ה-vacation guard נמצא במקום אחר; כאן רק מחזירים pending
        res = decide_appointment_status(_input(mode="manual", vacation_active=True))
        assert res.decision == "pending"


# ─── #9 ערב חג עם שעות חלקיות (special_day פתוח עם שעות מקוצרות) ────────
class TestErevChagShortHours:
    def test_slot_within_short_hours_ok(self):
        bh = _open_status(open_time="09:00", close_time="13:00")
        bh["source"] = "special_day"
        inp = _input(slot_time=time(11, 0), duration_minutes=60, business_hours_status=bh)
        res = decide_appointment_status(inp)
        assert res.decision == "confirmed"

    def test_slot_exceeds_short_close_rejected(self):
        bh = _open_status(open_time="09:00", close_time="13:00")
        inp = _input(slot_time=time(12, 30), duration_minutes=60, business_hours_status=bh)
        res = decide_appointment_status(inp)
        assert res.decision == "rejected"
        assert res.reason == "exceeds_closing_time"


# ─── #10 חציית חצות ──────────────────────────────────────────────────────
class TestCrossesMidnight:
    def test_rejected(self):
        bh = _open_status(open_time="08:00", close_time="23:59")
        inp = _input(slot_time=time(23, 30), duration_minutes=60, business_hours_status=bh)
        res = decide_appointment_status(inp)
        assert res.decision == "rejected"
        assert res.reason == "slot_crosses_midnight"


# ─── #11 חורג מסגירה ─────────────────────────────────────────────────────
class TestExceedsClosing:
    def test_rejected(self):
        bh = _open_status(open_time="09:00", close_time="18:00")
        inp = _input(slot_time=time(17, 30), duration_minutes=60, business_hours_status=bh)
        res = decide_appointment_status(inp)
        assert res.decision == "rejected"
        assert res.reason == "exceeds_closing_time"

    def test_exactly_at_close_ok(self):
        bh = _open_status(open_time="09:00", close_time="18:00")
        inp = _input(
            slot_time=time(17, 0), duration_minutes=60,
            business_hours_status=bh,
            available_slots=["16:00", "17:00"],
        )
        res = decide_appointment_status(inp)
        assert res.decision == "confirmed"

    def test_before_open_rejected(self):
        bh = _open_status(open_time="09:00", close_time="18:00")
        inp = _input(slot_time=time(8, 0), duration_minutes=30, business_hours_status=bh)
        res = decide_appointment_status(inp)
        assert res.decision == "rejected"
        assert res.reason == "before_business_hours"


# ─── #12 GCal לא מחובר ───────────────────────────────────────────────────
class TestCalendarNotConnected:
    def test_auto_with_check_falls_back_to_pending(self):
        res = decide_appointment_status(_input(calendar_connected=False, available_slots=None))
        assert res.decision == "pending"
        assert res.reason == "calendar_not_connected"

    def test_auto_always_still_confirms(self):
        res = decide_appointment_status(_input(
            mode="auto_always", calendar_connected=False, available_slots=None,
        ))
        assert res.decision == "confirmed"


# ─── #13 GCal busy ──────────────────────────────────────────────────────
class TestCalendarBusy:
    def test_rejected_when_slot_not_available(self):
        # 10:00 ביקשנו, אבל הוא לא ברשימת הסלוטים הפנויים
        inp = _input(slot_time=time(10, 0), available_slots=["09:00", "11:00"])
        res = decide_appointment_status(inp)
        assert res.decision == "rejected"
        assert res.reason == "calendar_busy"


# ─── #14 GCal timeout/error ─────────────────────────────────────────────
class TestCalendarCheckFailed:
    def test_falls_back_to_pending(self):
        inp = _input(calendar_check_failed=True, available_slots=None)
        res = decide_appointment_status(inp)
        assert res.decision == "pending"
        assert res.reason == "calendar_check_failed"


# ─── #15 תור קיים של אותו לקוח באותו יום ───────────────────────────────
class TestSameDayConflict:
    def test_falls_back_to_pending_for_review(self):
        inp = _input(user_has_appointment_same_day=True)
        res = decide_appointment_status(inp)
        assert res.decision == "pending"
        assert res.reason == "user_other_appointment_same_day"


# ─── #18 משך לא תקין ─────────────────────────────────────────────────────
class TestInvalidDuration:
    def test_zero_duration_rejected(self):
        inp = _input(duration_minutes=0)
        res = decide_appointment_status(inp)
        assert res.decision == "rejected"
        assert res.reason == "invalid_duration"


# ─── #20 קונפליקט עם תור קיים בסלוט ─────────────────────────────────────
class TestSlotTaken:
    def test_rejected_in_all_modes(self):
        for mode in ("manual", "auto_with_check", "auto_always"):
            inp = _input(mode=mode, has_pending_or_confirmed_conflict=True)
            res = decide_appointment_status(inp)
            assert res.decision == "rejected"
            assert res.reason == "slot_already_taken"


# ─── מצב לא תקין ─────────────────────────────────────────────────────────
class TestInvalidMode:
    def test_unknown_mode_falls_back_to_pending(self):
        inp = _input(mode="bogus")
        res = decide_appointment_status(inp)
        assert res.decision == "pending"
        assert res.reason == "invalid_mode_fallback"


# ─── שעות עבודה לא תקינות ───────────────────────────────────────────────
class TestInvalidBusinessHours:
    def test_falls_back_to_pending(self):
        bh = {
            "is_open": True,
            "open_time": "abc",  # ערך לא תקין
            "close_time": "18:00",
            "source": "regular",
        }
        inp = _input(business_hours_status=bh)
        res = decide_appointment_status(inp)
        assert res.decision == "pending"
        assert res.reason == "invalid_business_hours"


# ─── business_hours_status=None (orchestrator נכשל) ─────────────────────
class TestMissingBusinessHoursStatus:
    def test_auto_with_check_falls_back_to_pending(self):
        # gather_and_decide יכול להחזיר None אם get_status_for_date נכשל.
        # חוסר ודאות ⇒ pending, לא rejected. בלי זה — לקוח יקבל "סגור" שגוי.
        inp = _input(business_hours_status=None)
        res = decide_appointment_status(inp)
        assert res.decision == "pending"
        assert res.reason == "business_hours_unknown"

    def test_manual_unaffected(self):
        # ב-manual לא בודקים business_hours בכלל — תמיד pending.
        inp = _input(mode="manual", business_hours_status=None)
        res = decide_appointment_status(inp)
        assert res.decision == "pending"
        assert res.reason == "manual_mode"

    def test_auto_always_unaffected(self):
        # ב-auto_always לא בודקים business_hours — מאשרים אם אין vacation.
        inp = _input(mode="auto_always", business_hours_status=None)
        res = decide_appointment_status(inp)
        assert res.decision == "confirmed"


# ─── slot_time כמחרוזת (תמיכה בקלט גמיש) ───────────────────────────────
class TestStringSlotTime:
    def test_accepts_hh_mm_string(self):
        inp = _input(slot_time="10:00")
        res = decide_appointment_status(inp)
        assert res.decision == "confirmed"

    def test_accepts_hh_mm_ss_string(self):
        inp = _input(slot_time="10:00:00")
        res = decide_appointment_status(inp)
        assert res.decision == "confirmed"


# ─── הודעות דחייה בעברית ────────────────────────────────────────────────
class TestRejectionMessages:
    def test_known_reasons_have_hebrew_messages(self):
        for reason in (
            "slot_in_past", "slot_too_far_ahead", "slot_already_taken",
            "slot_crosses_midnight", "vacation_active", "closed_holiday",
            "calendar_busy",
        ):
            msg = get_rejection_message(reason)
            assert msg, f"empty message for {reason}"
            # בדיקת עברית — צריכה להיות לפחות אות עברית אחת
            assert any("א" <= c <= "ת" for c in msg)

    def test_unknown_reason_has_fallback(self):
        msg = get_rejection_message("totally_unknown_reason")
        assert msg
        # Fallback אינו ריק ובעברית
        assert any("א" <= c <= "ת" for c in msg)
