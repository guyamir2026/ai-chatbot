"""
טסטים למודול google_calendar — OAuth, FreeBusy, יצירת/מחיקת אירועים.
"""

import os
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake-token")
os.environ.setdefault("OPENAI_API_KEY", "fake-key")
os.environ.setdefault("GOOGLE_CLIENT_ID", "fake-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "fake-client-secret")
os.environ.setdefault("GOOGLE_REDIRECT_URI", "http://localhost/google/callback")

import sys
import types
from datetime import date, datetime, time, timedelta
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo

import pytest

ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")

# מוק ל-google packages — לא מותקנים בסביבת טסטים
_google_mock_modules = {}
for mod_name in [
    "google", "google.oauth2", "google.oauth2.credentials",
    "google.auth", "google.auth.transport", "google.auth.transport.requests",
    "google_auth_oauthlib", "google_auth_oauthlib.flow",
    "googleapiclient", "googleapiclient.discovery", "googleapiclient.errors",
]:
    if mod_name not in sys.modules:
        m = types.ModuleType(mod_name)
        sys.modules[mod_name] = m
        _google_mock_modules[mod_name] = m

# הוספת classes חיוניים
sys.modules["google.oauth2.credentials"].Credentials = MagicMock()
sys.modules["google_auth_oauthlib.flow"].Flow = MagicMock()
sys.modules["googleapiclient.discovery"].build = MagicMock()

# HttpError — צריך להיות exception אמיתי
class _MockHttpError(Exception):
    def __init__(self, resp=None, content=b""):
        self.resp = resp or MagicMock(status=400)
        self.content = content
        super().__init__("HttpError")

sys.modules["googleapiclient.errors"].HttpError = _MockHttpError


@pytest.fixture
def db(tmp_path):
    """DB בקובץ זמני עם סכימה מלאה."""
    db_path = tmp_path / "test.db"
    with patch("ai_chatbot.config.DB_PATH", db_path):
        import importlib
        import database
        importlib.reload(database)
        database.init_db()
        yield database


class TestDatabaseGoogleCalendar:
    """טסטים לפונקציות DB של Google Calendar."""

    def test_save_and_get_credentials(self, db):
        """שמירה ושליפה של credentials."""
        db.save_google_calendar_credentials(
            google_account_email="test@gmail.com",
            calendar_id="primary",
            refresh_token="refresh-123",
            access_token="access-456",
            token_expiry="2026-01-01T00:00:00",
            timezone="Asia/Jerusalem",
        )
        cred = db.get_google_calendar_credentials()
        assert cred is not None
        assert cred["google_account_email"] == "test@gmail.com"
        assert cred["refresh_token"] == "refresh-123"
        assert cred["access_token"] == "access-456"
        assert cred["timezone"] == "Asia/Jerusalem"

    def test_get_credentials_returns_none_when_empty(self, db):
        """מחזיר None כשאין credentials."""
        cred = db.get_google_calendar_credentials()
        assert cred is None

    def test_update_token(self, db):
        """עדכון access token."""
        db.save_google_calendar_credentials(
            google_account_email="test@gmail.com",
            calendar_id="primary",
            refresh_token="refresh-123",
            access_token="old-token",
            token_expiry="2026-01-01T00:00:00",
            timezone="Asia/Jerusalem",
        )
        db.update_google_calendar_token("new-token", "2026-06-01T00:00:00")
        cred = db.get_google_calendar_credentials()
        assert cred["access_token"] == "new-token"
        assert cred["token_expiry"] == "2026-06-01T00:00:00"

    def test_delete_credentials(self, db):
        """מחיקת credentials (ניתוק)."""
        db.save_google_calendar_credentials(
            google_account_email="test@gmail.com",
            calendar_id="primary",
            refresh_token="refresh-123",
            access_token="access-456",
            token_expiry="",
            timezone="Asia/Jerusalem",
        )
        db.delete_google_calendar_credentials()
        cred = db.get_google_calendar_credentials()
        assert cred is None

    def test_set_appointment_google_event_id(self, db):
        """שמירת google_event_id לתור."""
        appt_id = db.create_appointment(
            user_id="u1", username="ישראל",
            service="תספורת", preferred_date="2026-04-15",
            preferred_time="10:00",
        )
        db.set_appointment_google_event_id(appt_id, "gcal_event_123")
        appt = db.get_appointment(appt_id)
        assert appt["google_event_id"] == "gcal_event_123"


class TestGoogleCalendarModule:
    """טסטים ללוגיקה של google_calendar.py (עם מוקים ל-API)."""

    def test_is_connected_false_when_no_creds(self, db):
        """is_connected מחזיר False כשאין credentials."""
        with patch("google_calendar.db", db):
            from google_calendar import is_connected
            assert is_connected() is False

    def test_is_connected_true_with_creds(self, db):
        """is_connected מחזיר True כשיש credentials."""
        db.save_google_calendar_credentials(
            google_account_email="test@gmail.com",
            calendar_id="primary",
            refresh_token="refresh-123",
            access_token="access-456",
            token_expiry="",
            timezone="Asia/Jerusalem",
        )
        with patch("google_calendar.db", db):
            from google_calendar import is_connected
            assert is_connected() is True

    def test_get_available_slots_no_connection(self, db):
        """get_available_slots זורק CalendarUnavailable כשהשירות לא זמין.

        החוזה הזה מבדיל בין "אין סלוטים תפוסים" (FreeBusy החזיר [])
        לבין "אי-אפשר לבדוק" (token שבור / שגיאת רשת). הקוראים מטפלים
        בחריג: gather_and_decide → calendar_check_failed=True (pending),
        compute_duration_options → except Exception (דילוג), וכו'.
        """
        with patch("google_calendar.db", db), \
             patch("google_calendar._get_calendar_service", return_value=None), \
             patch("business_hours.get_status_for_date", return_value={
                 "is_open": True, "open_time": "09:00", "close_time": "17:00",
             }):
            from google_calendar import get_available_slots, CalendarUnavailable
            target = date(2026, 4, 15)
            with pytest.raises(CalendarUnavailable):
                get_available_slots(target)

    def test_get_available_slots_closed_day(self, db):
        """get_available_slots מחזיר רשימה ריקה ליום סגור."""
        with patch("google_calendar.db", db), \
             patch("business_hours.get_status_for_date", return_value={
                 "is_open": False, "reason": "שבת",
             }):
            from google_calendar import get_available_slots
            target = date(2026, 4, 15)
            slots = get_available_slots(target)
            assert slots == []

    def test_get_available_slots_excludes_own_appointment(self, db):
        """רגרסיה: תור שזה עתה נוצר לא חוסם את השעה של עצמו.

        זהו שורש הבאג 'אוטומטי עם בדיקה ⇒ כל שעה תפוסה': ההחלטה רצה
        *אחרי* create_appointment, ו-get_available_slots ספר את התור-עצמו
        כטווח DB תפוס והסיר את השעה המבוקשת. exclude_appointment_id מתקן
        זאת — סימטרי לבדיקת הקונפליקט ב-DB שסופרת c >= 2.
        """
        appt_id = db.create_appointment(
            user_id="u1", username="ישראל",
            service="תספורת", preferred_date="2099-05-06",
            preferred_time="09:30",
        )
        with patch("google_calendar.db", db), \
             patch("google_calendar.get_busy_slots", return_value=[]), \
             patch("google_calendar._get_calendar_service", return_value=MagicMock()), \
             patch("business_hours.get_status_for_date", return_value={
                 "is_open": True, "open_time": "09:00", "close_time": "17:00",
             }):
            from google_calendar import get_available_slots
            target = date(2099, 5, 6)
            # בלי exclude — התור-עצמו חוסם את 09:30 (השורש של הבאג)
            slots_without = get_available_slots(target, service_duration_minutes=60)
            assert "09:30" not in slots_without
            # עם exclude — 09:30 שוב פנוי, וכך ההחלטה תאשר במקום לדחות
            slots_with = get_available_slots(
                target, service_duration_minutes=60,
                exclude_appointment_id=appt_id,
            )
            assert "09:30" in slots_with

    def test_create_event_no_connection(self, db):
        """create_event מחזיר None כשאין חיבור."""
        with patch("google_calendar.db", db), \
             patch("google_calendar._get_calendar_service", return_value=None):
            from google_calendar import create_event
            result = create_event(
                appt_id=1, service="תספורת", customer_name="ישראל",
                start_dt=datetime(2026, 4, 15, 10, 0, tzinfo=ISRAEL_TZ),
                end_dt=datetime(2026, 4, 15, 11, 0, tzinfo=ISRAEL_TZ),
            )
            assert result is None

    def test_delete_event_no_connection(self, db):
        """delete_event מחזיר False כשאין חיבור."""
        with patch("google_calendar.db", db), \
             patch("google_calendar._get_calendar_service", return_value=None):
            from google_calendar import delete_event
            assert delete_event("some-event-id") is False

    def test_sync_confirmed_creates_event(self, db):
        """sync_appointment_to_calendar יוצר אירוע כשתור מאושר."""
        db.save_google_calendar_credentials(
            google_account_email="test@gmail.com",
            calendar_id="primary",
            refresh_token="refresh-123",
            access_token="access-456",
            token_expiry="",
            timezone="Asia/Jerusalem",
        )
        appt = {
            "id": 1,
            "service": "תספורת",
            "username": "ישראל",
            "preferred_date": "2026-04-15",
            "preferred_time": "10:00",
            "google_event_id": "",
        }
        with patch("google_calendar.db", db), \
             patch("google_calendar.create_event", return_value="gcal_123") as mock_create:
            from google_calendar import sync_appointment_to_calendar
            sync_appointment_to_calendar(appt, "confirmed")
            mock_create.assert_called_once()

    def test_sync_cancelled_deletes_event(self, db):
        """sync_appointment_to_calendar מוחק אירוע כשתור מבוטל."""
        db.save_google_calendar_credentials(
            google_account_email="test@gmail.com",
            calendar_id="primary",
            refresh_token="refresh-123",
            access_token="access-456",
            token_expiry="",
            timezone="Asia/Jerusalem",
        )
        appt = {
            "id": 1,
            "service": "תספורת",
            "username": "ישראל",
            "preferred_date": "2026-04-15",
            "preferred_time": "10:00",
            "google_event_id": "gcal_123",
        }
        with patch("google_calendar.db", db), \
             patch("google_calendar.delete_event", return_value=True) as mock_del:
            from google_calendar import sync_appointment_to_calendar
            sync_appointment_to_calendar(appt, "cancelled")
            mock_del.assert_called_once_with("gcal_123")

    def test_disconnect_clears_credentials(self, db):
        """disconnect_calendar מנקה credentials."""
        db.save_google_calendar_credentials(
            google_account_email="test@gmail.com",
            calendar_id="primary",
            refresh_token="refresh-123",
            access_token="access-456",
            token_expiry="",
            timezone="Asia/Jerusalem",
        )
        with patch("google_calendar.db", db):
            from google_calendar import disconnect_calendar
            disconnect_calendar()
        cred = db.get_google_calendar_credentials()
        assert cred is None


class TestGatherAndDecideExcludesSelf:
    """רגרסיה end-to-end לבאג שהמשתמש דיווח עליו:
    'אוטומטי עם בדיקה ⇒ כל תאריך שמנסים לקבוע, הבוט אומר שהשעה תפוסה'.

    מדמה את הזרימה האמיתית: create_appointment קורה *לפני* gather_and_decide,
    ולכן בלי exclude_appointment_id ההחלטה סופרת את התור-עצמו כטווח תפוס
    ומחזירה rejected(calendar_busy) לכל שעה. הבדיקה מוודאת ששני הקצוות
    עובדים — בלי exclude נדחה (מוכיח את השורש), ועם exclude מאושר (מוכיח
    את התיקון).
    """

    def _future_date(self):
        # תאריך בתוך חלון max_days_ahead (90) כדי לא ליפול ל-slot_too_far_ahead,
        # ולא היום עצמו כדי לא להיחתך ע"י cutoff ה-'now' ב-get_available_slots.
        return date.today() + timedelta(days=7)

    def test_created_appointment_does_not_block_itself(self, db):
        db.update_bot_settings(tone="friendly", auto_booking_mode="auto_with_check")
        db.save_google_calendar_credentials(
            google_account_email="test@gmail.com",
            calendar_id="primary",
            refresh_token="refresh-123",
            access_token="access-456",
            token_expiry="",
            timezone="Asia/Jerusalem",
        )

        target = self._future_date()
        target_iso = target.isoformat()
        appt_id = db.create_appointment(
            user_id="u1", username="ישראל",
            service="תספורת", preferred_date=target_iso,
            preferred_time="09:30",
        )

        open_status = {
            "is_open": True, "open_time": "09:00", "close_time": "17:00",
            "source": "regular",
        }
        # gather_and_decide ניגש ל-business_hours דרך wrapper ai_chatbot,
        # ו-get_available_slots דרך המודול בשורש — לכן מטליאים את שניהם.
        with patch("google_calendar.db", db), \
             patch("google_calendar.is_connected", return_value=True), \
             patch("google_calendar.get_busy_slots", return_value=[]), \
             patch("google_calendar._get_calendar_service", return_value=MagicMock()), \
             patch("business_hours.get_status_for_date", return_value=open_status), \
             patch("ai_chatbot.business_hours.get_status_for_date", return_value=open_status):
            from ai_chatbot.core.booking_decision import gather_and_decide

            # בלי exclude — התור-עצמו חוסם ⇒ rejected calendar_busy (הבאג)
            buggy = gather_and_decide(
                user_id="u1", slot_date_str=target_iso, slot_time_str="09:30",
            )
            assert buggy.decision == "rejected"
            assert buggy.reason == "calendar_busy"

            # עם exclude — התור-עצמו מוחרג ⇒ confirmed (התיקון)
            fixed = gather_and_decide(
                user_id="u1", slot_date_str=target_iso, slot_time_str="09:30",
                exclude_appointment_id=appt_id,
            )
            assert fixed.decision == "confirmed"
            assert fixed.reason == "auto_with_check_ok"
