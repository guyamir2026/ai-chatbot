"""
טסטים ל-WhatsApp Booking Flow — state machine, בחירה מספרית, flow מלא.

מוקים: DB, send_whatsapp, Google Calendar.
"""

import os
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def db(tmp_path):
    """DB זמני למחיקה."""
    db_path = tmp_path / "test.db"
    with patch("ai_chatbot.config.DB_PATH", db_path):
        import importlib
        import database
        importlib.reload(database)
        database.init_db()
        yield database


@pytest.fixture
def state():
    """conversation_state עם ניקוי בין טסטים."""
    from messaging.conversation_state import _sessions
    _sessions.clear()
    from messaging import conversation_state
    yield conversation_state
    _sessions.clear()


@pytest.fixture
def booking(db):
    """WhatsApp booking module עם mock ל-send ו-notifications."""
    with patch("messaging.whatsapp_booking.send_whatsapp"), \
         patch("messaging.whatsapp_booking.TELEGRAM_OWNER_CHAT_ID", ""):
        from messaging import whatsapp_booking
        yield whatsapp_booking


# ── Conversation State Tests ────────────────────────────────────────────────


class TestConversationState:
    def test_set_and_get_state(self, state):
        state.set_state("user1", "booking_service", {"key": "val"})
        session = state.get_state("user1")
        assert session is not None
        assert session["state"] == "booking_service"
        assert session["data"]["key"] == "val"

    def test_get_state_none_for_unknown_user(self, state):
        assert state.get_state("unknown") is None

    def test_clear_state(self, state):
        state.set_state("user1", "booking_service")
        state.clear_state("user1")
        assert state.get_state("user1") is None

    def test_state_timeout(self, state):
        """session שפג תוקפו מוחזר כ-None."""
        import time
        state.set_state("user1", "booking_service")
        # הזקנת ה-session ידנית
        state._sessions["user1"]["updated_at"] = time.time() - 3600
        assert state.get_state("user1") is None

    def test_set_state_merges_data(self, state):
        """set_state משמר data קודם וממזג עם חדש."""
        state.set_state("user1", "booking_service", {"service": "תספורת"})
        state.set_state("user1", "booking_date", {"date": "2026-04-10"})
        session = state.get_state("user1")
        assert session["data"]["service"] == "תספורת"
        assert session["data"]["date"] == "2026-04-10"
        assert session["state"] == "booking_date"

    def test_cleanup_expired(self, state):
        import time
        state.set_state("user1", "booking_service")
        state.set_state("user2", "booking_date")
        state._sessions["user1"]["updated_at"] = time.time() - 3600
        cleaned = state.cleanup_expired()
        assert cleaned == 1
        assert state.get_state("user1") is None
        assert state.get_state("user2") is not None


# ── Start Booking Tests ─────────────────────────────────────────────────────


class TestStartBooking:
    def test_start_with_services(self, booking, db):
        """start_booking עם שירותים מוגדרים — מחזיר רשימה ממוספרת."""
        db.add_service("תספורת גברים", 30)
        db.add_service("תספורת נשים", 45)

        result = booking.start_booking("+972501234567")
        assert "בחרו שירות" in result
        assert "1." in result
        assert "2." in result
        assert "תספורת גברים" in result
        assert "תספורת נשים" in result

    def test_start_without_services(self, booking, db):
        """start_booking ללא שירותים — הודעת fallback."""
        result = booking.start_booking("+972501234567")
        assert "אין שירותים" in result


# ── Full Booking Flow Tests ──────────────────────────────────────────────────


class TestFullBookingFlow:
    def test_numeric_service_selection(self, booking, db):
        """בחירת שירות לפי מספר."""
        db.add_service("תספורת", 30)
        booking.start_booking("+972501234567")

        result = booking.handle_booking_step("+972501234567", "1")
        assert "תספורת" in result
        assert "תאריך" in result

    def test_invalid_service_selection(self, booking, db):
        """בחירה לא תקינה — הצגת הרשימה שוב."""
        db.add_service("תספורת", 30)
        booking.start_booking("+972501234567")

        result = booking.handle_booking_step("+972501234567", "99")
        assert "לא זיהיתי" in result

    def test_date_input(self, booking, db):
        """קלט תאריך תקין."""
        db.add_service("תספורת", 30)
        booking.start_booking("+972501234567")
        booking.handle_booking_step("+972501234567", "1")

        with patch("messaging.whatsapp_booking.normalize_date", return_value="2026-04-15"):
            result = booking.handle_booking_step("+972501234567", "15/04")
        assert "שעה" in result
        assert "15/04/2026" in result

    def test_invalid_date_input(self, booking, db):
        """קלט תאריך לא תקין — מבקש שוב."""
        db.add_service("תספורת", 30)
        booking.start_booking("+972501234567")
        booking.handle_booking_step("+972501234567", "1")

        with patch("messaging.whatsapp_booking.normalize_date", return_value=None):
            result = booking.handle_booking_step("+972501234567", "xxx")
        assert "לא הצלחתי לזהות תאריך" in result

    def test_time_input_shows_confirmation(self, booking, db):
        """קלט שעה — הצגת סיכום לאישור."""
        db.add_service("תספורת", 30)
        booking.start_booking("+972501234567")
        booking.handle_booking_step("+972501234567", "1")

        with patch("messaging.whatsapp_booking.normalize_date", return_value="2026-04-15"):
            booking.handle_booking_step("+972501234567", "15/04")

        result = booking.handle_booking_step("+972501234567", "14:00")
        assert "סיכום" in result
        assert "תספורת" in result
        assert "14:00" in result
        assert "כן" in result

    def test_confirmation_creates_appointment(self, booking, db):
        """אישור — יצירת תור ב-DB."""
        # תאריך עתידי דינמי — תאריך קבוע בעבר גורם ל-auto-booking decision
        # להחזיר rejected (slot_in_past) ולבטל את התור.
        from datetime import date, timedelta
        future_date = (date.today() + timedelta(days=7)).isoformat()

        db.add_service("תספורת", 30)
        booking.start_booking("+972501234567")
        booking.handle_booking_step("+972501234567", "1")

        with patch("messaging.whatsapp_booking.normalize_date", return_value=future_date):
            booking.handle_booking_step("+972501234567", "15/04")
        booking.handle_booking_step("+972501234567", "14:00")

        result = booking.handle_booking_step("+972501234567", "כן")
        assert "התקבלה" in result

        # בדיקה ב-DB
        appts = db.get_appointments()
        assert len(appts) == 1
        assert appts[0]["service"] == "תספורת"
        assert appts[0]["preferred_date"] == future_date
        assert appts[0]["preferred_time"] == "14:00"
        assert appts[0].get("channel") == "whatsapp"

    def test_confirmation_no_cancels(self, booking, db):
        """תשובה שלילית — ביטול."""
        db.add_service("תספורת", 30)
        booking.start_booking("+972501234567")
        booking.handle_booking_step("+972501234567", "1")

        with patch("messaging.whatsapp_booking.normalize_date", return_value="2026-04-15"):
            booking.handle_booking_step("+972501234567", "15/04")
        booking.handle_booking_step("+972501234567", "14:00")

        result = booking.handle_booking_step("+972501234567", "לא")
        assert "בוטלה" in result

        # לא נוצר תור
        assert len(db.get_appointments()) == 0

    def test_cancel_at_any_step(self, booking, db):
        """ביטול באמצע ה-flow."""
        db.add_service("תספורת", 30)
        booking.start_booking("+972501234567")

        result = booking.handle_booking_step("+972501234567", "ביטול")
        assert "בוטל" in result

    def test_no_state_returns_none(self, booking, db):
        """כשאין state פתוח — מחזיר None."""
        result = booking.handle_booking_step("+972501234567", "שלום")
        assert result is None


# ── Duplicate Appointment Protection ──────────────────────────────────────────


class TestDuplicateProtection:
    def test_duplicate_appointment_blocked(self, booking, db):
        """תור כפול נחסם."""
        from datetime import date, timedelta
        future_date = (date.today() + timedelta(days=7)).isoformat()

        db.add_service("תספורת", 30)

        # תור ראשון — מלא
        booking.start_booking("+972501234567")
        booking.handle_booking_step("+972501234567", "1")
        with patch("messaging.whatsapp_booking.normalize_date", return_value=future_date):
            booking.handle_booking_step("+972501234567", "15/04")
        booking.handle_booking_step("+972501234567", "14:00")
        booking.handle_booking_step("+972501234567", "כן")

        # תור שני — אותו מועד
        booking.start_booking("+972501234567")
        booking.handle_booking_step("+972501234567", "1")
        with patch("messaging.whatsapp_booking.normalize_date", return_value=future_date):
            booking.handle_booking_step("+972501234567", "15/04")
        booking.handle_booking_step("+972501234567", "14:00")
        result = booking.handle_booking_step("+972501234567", "כן")
        assert "כבר יש" in result

        # רק תור אחד ב-DB
        assert len(db.get_appointments()) == 1


# ── דחיית auto-booking — נשארים ב-flow ומאזינים לתיקון ────────────────────────


class TestRejectionKeepsFlowAlive:
    """רגרסיה: אחרי דחיית תור הבוט נשאר ב-flow בשלב המתאים ומאזין לשעה/תאריך
    החדש, במקום לנקות state ולהשאיר את "בחרו אחר/ת" בלי מאזין (⇒ הלקוח נפל
    ל-RAG "לא הבנתי").
    """

    USER = "+972501234567"

    def _advance_to_confirm(self, booking, db):
        """מתקדם עד שלב האישור (confirm) עם תאריך עתידי ושעה 14:00."""
        from datetime import date, timedelta
        future_date = (date.today() + timedelta(days=7)).isoformat()
        db.add_service("תספורת", 30)
        booking.start_booking(self.USER)
        booking.handle_booking_step(self.USER, "1")
        with patch("messaging.whatsapp_booking.normalize_date", return_value=future_date):
            booking.handle_booking_step(self.USER, "15/04")
        booking.handle_booking_step(self.USER, "14:00")
        return future_date

    def _reject_with(self, reason):
        """context manager שמכריח את gather_and_decide להחזיר rejected(reason)."""
        from core.booking_decision import BookingDecisionResult
        return patch(
            "ai_chatbot.core.booking_decision.gather_and_decide",
            return_value=BookingDecisionResult("rejected", reason),
        )

    def test_time_rejection_returns_to_time_step(self, booking, db, state):
        """דחיית שעה (calendar_busy) ⇒ נשארים ב-STATE_BOOKING_TIME, service/date נשמרים."""
        self._advance_to_confirm(booking, db)
        with self._reject_with("calendar_busy"):
            result = booking.handle_booking_step(self.USER, "כן")

        assert "תפוסה" in result  # הודעת calendar_busy
        session = state.get_state(self.USER)
        assert session is not None, "ה-state לא אמור להימחק — הבוט חייב להמשיך להאזין"
        assert session["state"] == state.STATE_BOOKING_TIME
        assert state.get_session_data(self.USER, "booking_service") == "תספורת"

    def test_time_rejection_listens_to_next_time(self, booking, db, state):
        """אחרי דחייה, קלט שעה חדש מטופל ע"י ה-flow (לא None, לא נופל ל-RAG)."""
        self._advance_to_confirm(booking, db)
        with self._reject_with("calendar_busy"):
            booking.handle_booking_step(self.USER, "כן")

        # השעה החדשה — מטופלת בזרימה ומחזירה סיכום (מכיל את השעה)
        result = booking.handle_booking_step(self.USER, "16:00")
        assert result is not None, "השעה החדשה נפלה מחוץ ל-flow (הבאג)"
        assert "16:00" in result

    def test_date_rejection_returns_to_date_step(self, booking, db, state):
        """דחיית יום סגור (closed_regular) ⇒ נשארים ב-STATE_BOOKING_DATE."""
        self._advance_to_confirm(booking, db)
        with self._reject_with("closed_regular"):
            booking.handle_booking_step(self.USER, "כן")

        session = state.get_state(self.USER)
        assert session is not None
        assert session["state"] == state.STATE_BOOKING_DATE

    def test_terminal_rejection_clears_and_hints_restart(self, booking, db, state):
        """דחייה סופית (vacation_active) ⇒ state נוקה + הצעה להתחיל מחדש."""
        self._advance_to_confirm(booking, db)
        with self._reject_with("vacation_active"):
            result = booking.handle_booking_step(self.USER, "כן")

        assert state.get_state(self.USER) is None, "סיבה סופית — ה-state אמור להתנקות"
        assert "לנסות שוב" in result

    def test_recheck_slot_taken_returns_to_time_with_slots(self, booking, db, state):
        """הבדיקה החוזרת (לפני יצירת התור): השעה כבר לא פנויה ⇒ נשארים בשלב
        השעה ומציגים את השעות הפנויות שחושבו.
        """
        import sys
        import types
        fake_gcal = types.ModuleType("google_calendar")
        fake_gcal.is_connected = lambda: True
        # 14:00 לא ברשימה ⇒ יופעל מסלול "כבר לא פנויה"
        fake_gcal.get_available_slots = lambda *a, **k: ["09:00", "10:30"]
        with patch.dict(sys.modules, {"google_calendar": fake_gcal}):
            self._advance_to_confirm(booking, db)
            result = booking.handle_booking_step(self.USER, "כן")

        session = state.get_state(self.USER)
        assert session is not None
        assert session["state"] == state.STATE_BOOKING_TIME
        assert "09:00" in result and "10:30" in result
        # הבדיקה החוזרת קורית לפני create_appointment ⇒ לא נוצר תור
        assert len(db.get_appointments()) == 0

    def test_recheck_no_slots_returns_to_date(self, booking, db, state):
        """הבדיקה החוזרת: אין שעות פנויות ביום כלל (רשימה ריקה) ⇒ חוזרים
        לבחירת *תאריך*, ולא נתקעים בשלב השעה שבו אף שעה לא תעבוד.
        """
        import sys
        import types
        # מתקדמים לאישור בלי GCal (ה-import נכשל בסביבת הטסט ⇒ שלב התאריך
        # מדלג על בדיקת הזמינות ומתקדם), ואז מזריקים GCal ריק רק לצעד האישור.
        self._advance_to_confirm(booking, db)
        fake_gcal = types.ModuleType("google_calendar")
        fake_gcal.is_connected = lambda: True
        fake_gcal.get_available_slots = lambda *a, **k: []
        with patch.dict(sys.modules, {"google_calendar": fake_gcal}):
            result = booking.handle_booking_step(self.USER, "כן")

        session = state.get_state(self.USER)
        assert session is not None, "ה-state לא אמור להימחק"
        # אין שעות ⇒ חוזרים לשלב התאריך, לא נשארים בשלב השעה
        assert session["state"] == state.STATE_BOOKING_DATE
        assert "אין שעות פנויות" in result
        assert "תאריך אחר" in result
        # הבדיקה החוזרת קורית לפני create_appointment ⇒ לא נוצר תור
        assert len(db.get_appointments()) == 0
