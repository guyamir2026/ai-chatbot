"""
טסטים ל-compute_duration_options_for_appointment + ההגדרות שלו.

הלוגיקה הנבדקת:
- אופציות משך נבנות סביב ברירת המחדל של השירות, לפי step + backward + forward
- אופציות שגולשות אחרי close_time מסוננות
- אופציות שמתנגשות עם תורים אחרים (pending/confirmed) באותו יום מסוננות
- תורים cancelled/passed לא חוסמים
- confirmed_duration_minutes של תורים אחרים נלקח בחשבון
- אם אין אף אופציה תקינה — מחזירים את ברירת המחדל לפחות
"""

import os
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake-token")
os.environ.setdefault("OPENAI_API_KEY", "fake-key")
os.environ.setdefault("GOOGLE_CLIENT_ID", "fake-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "fake-client-secret")
os.environ.setdefault("GOOGLE_REDIRECT_URI", "http://localhost/google/callback")

import sys
import types
from unittest.mock import patch, MagicMock

import pytest

# מוק ל-google packages — לא מותקנים בסביבת טסטים
for _mod_name in [
    "google", "google.oauth2", "google.oauth2.credentials",
    "google.auth", "google.auth.transport", "google.auth.transport.requests",
    "google_auth_oauthlib", "google_auth_oauthlib.flow",
    "googleapiclient", "googleapiclient.discovery", "googleapiclient.errors",
]:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = types.ModuleType(_mod_name)
sys.modules["google.oauth2.credentials"].Credentials = MagicMock()
sys.modules["google_auth_oauthlib.flow"].Flow = MagicMock()
sys.modules["googleapiclient.discovery"].build = MagicMock()


class _MockHttpError(Exception):
    pass


sys.modules["googleapiclient.errors"].HttpError = _MockHttpError


@pytest.fixture
def db(tmp_path):
    """מאתחל DB בקובץ זמני ומחזיר את מודול database."""
    db_path = tmp_path / "test.db"
    with patch("ai_chatbot.config.DB_PATH", db_path):
        import importlib
        import database
        importlib.reload(database)
        database.init_db()
        yield database


def _create_pending(db, *, time: str, service: str = "טיפול", date: str = "2099-01-05"):
    """עוזר ליצירת תור pending — מחזיר id."""
    return db.create_appointment(
        user_id="u1", username="לקוח", service=service,
        preferred_date=date, preferred_time=time,
    )


class TestDurationSettings:
    def test_defaults(self, db):
        s = db.get_appointment_duration_settings()
        assert s["step_minutes"] == 15
        assert s["steps_backward"] == 2
        assert s["steps_forward"] == 4

    def test_update_settings(self, db):
        db.update_appointment_duration_settings(step_minutes=30, steps_backward=1, steps_forward=3)
        s = db.get_appointment_duration_settings()
        assert s["step_minutes"] == 30
        assert s["steps_backward"] == 1
        assert s["steps_forward"] == 3

    def test_clamps_to_safe_range(self, db):
        # ערכים מטורפים — מקליפים אותם לטווח בטוח
        db.update_appointment_duration_settings(step_minutes=999, steps_backward=-5, steps_forward=99)
        s = db.get_appointment_duration_settings()
        assert s["step_minutes"] == 120
        assert s["steps_backward"] == 0
        assert s["steps_forward"] == 10

    def test_short_call_preserves_default_minutes(self, db):
        """קריאה ל-update בלי default_minutes לא דורסת את הערך הקיים.
        נכשל כשcursor הצביע על הבאג: הarg היה default_minutes=60 והוא דרס.
        """
        # קודם שומרים default ספציפי
        db.update_appointment_duration_settings(
            step_minutes=15, steps_backward=2, steps_forward=4, default_minutes=90,
        )
        assert db.get_appointment_duration_settings()["default_minutes"] == 90
        # עכשיו קוראים שוב בלי default_minutes — אסור שיקרוס ל-60
        db.update_appointment_duration_settings(
            step_minutes=20, steps_backward=3, steps_forward=5,
        )
        s = db.get_appointment_duration_settings()
        assert s["default_minutes"] == 90  # לא נדרס
        assert s["step_minutes"] == 20
        assert s["steps_backward"] == 3
        assert s["steps_forward"] == 5


class TestComputeDurationOptions:
    def _patch_no_business_block(self):
        """מנטרל business_hours — מחזיר יום פתוח 24 שעות, ללא חסימת close."""
        return patch(
            "business_hours.get_status_for_date",
            return_value={"is_open": True, "open_time": "00:00", "close_time": "23:59"},
        )

    def _patch_no_gcal(self):
        """מנטרל GCal — לא מחובר."""
        return patch("google_calendar.is_connected", return_value=False)

    def test_no_conflicts_returns_full_range(self, db):
        db.add_service("טיפול", duration_minutes=60)
        appt_id = _create_pending(db, time="10:00")
        with self._patch_no_business_block(), self._patch_no_gcal():
            opts = db.compute_duration_options_for_appointment(appt_id)
        # 60 ± 2*15 לאחור ו-4*15 קדימה: 30, 45, 60, 75, 90, 105, 120
        assert opts == [30, 45, 60, 75, 90, 105, 120]

    def test_minimum_duration_filter(self, db):
        """אופציות שיורדות מתחת ל-5 דק׳ נחסמות.
        ברירת המחדל היא גלובלית — מגדירים אותה ל-10 כדי לבדוק את החתך.
        """
        db.update_appointment_duration_settings(
            step_minutes=15, steps_backward=3, steps_forward=2, default_minutes=10,
        )
        appt_id = _create_pending(db, time="10:00")
        with self._patch_no_business_block(), self._patch_no_gcal():
            opts = db.compute_duration_options_for_appointment(appt_id)
        # 10 - 3*15 = -35 (פסול), -20 (פסול), -5 (פסול), 10, 25, 40
        assert opts == [10, 25, 40]

    def test_close_time_blocks_long_options(self, db):
        """אופציות שגולשות מ-close_time נחסמות."""
        db.add_service("טיפול", duration_minutes=60)
        appt_id = _create_pending(db, time="16:00")
        with patch(
            "business_hours.get_status_for_date",
            return_value={"is_open": True, "open_time": "09:00", "close_time": "17:00"},
        ), self._patch_no_gcal():
            opts = db.compute_duration_options_for_appointment(appt_id)
        # תור ב-16:00, סוף יום 17:00 → רק אופציות עד 60 דק' (סיום ב-17:00).
        # 30, 45, 60 → תקינים. 75, 90, 105, 120 → גולשים.
        assert opts == [30, 45, 60]

    def test_other_pending_appointment_blocks_overlapping(self, db):
        """תור אחר pending באותו יום חוסם אופציות חופפות."""
        db.add_service("טיפול", duration_minutes=60)
        # יש תור ב-11:00 (60 דק') → תופס 11:00-12:00
        _create_pending(db, time="11:00")
        # תור חדש ב-10:00 — יכול להיות 30 או 60 דק׳ (עד 11:00), לא יותר
        appt_id = _create_pending(db, time="10:00")
        with self._patch_no_business_block(), self._patch_no_gcal():
            opts = db.compute_duration_options_for_appointment(appt_id)
        assert 60 in opts  # 10:00-11:00, צמוד ל-busy
        assert 75 not in opts  # 10:00-11:15, חופף
        assert 30 in opts
        assert 45 in opts

    def test_cancelled_appointment_does_not_block(self, db):
        """תור cancelled לא חוסם אופציות."""
        db.add_service("טיפול", duration_minutes=60)
        cancelled_id = _create_pending(db, time="11:00")
        db.update_appointment_status(cancelled_id, "cancelled")
        appt_id = _create_pending(db, time="10:00")
        with self._patch_no_business_block(), self._patch_no_gcal():
            opts = db.compute_duration_options_for_appointment(appt_id)
        # אין חסימה — כל הטווח זמין
        assert 120 in opts

    def test_confirmed_other_uses_confirmed_duration(self, db):
        """כשבעל העסק כבר אישר תור אחר עם משך מותאם — המשך הזה נחשב לחסימה."""
        db.add_service("טיפול", duration_minutes=60)
        # תור אחר ב-12:00 שאושר ל-30 דקות (12:00-12:30) במקום 60.
        # מרחיק אותו מ-11:00 כדי שיווצר חלון פנוי גדול שמראה את ההבדל.
        other = _create_pending(db, time="12:00")
        db.update_appointment_status(other, "confirmed", confirmed_duration_minutes=30)
        # תור חדש ב-10:00 — 120 דק׳ ימתחו 10:00-12:00 (מתחבר בדיוק) — תקין.
        # אם המערכת חשבה שהאחר תופס 60 דקות (12:00-13:00) זה לא משנה ל-10:00.
        # הבדיקה האמיתית: תור חדש ב-12:30 — ב-30 דקות זה 13:00 (תקין כי 12:30 פנוי
        # אחרי שה-busy של 12:00-12:30 נגמר). אם היה מתעדכן ל-60 דק', 12:30-13:00
        # היה חופף עם 12:00-13:00 וכל האופציות היו פסולות.
        appt_id = _create_pending(db, time="12:30")
        with self._patch_no_business_block(), self._patch_no_gcal():
            opts = db.compute_duration_options_for_appointment(appt_id)
        # 12:30 פנוי כי המשך המאושר הוא 30 דק׳ (12:00-12:30, לא 12:00-13:00)
        assert 30 in opts and 60 in opts

    def test_returns_default_when_all_blocked(self, db):
        """גם כשכל האופציות חסומות — מחזירים את ברירת המחדל לפחות."""
        db.add_service("טיפול", duration_minutes=60)
        # יוצרים תור צמוד מאחור וקדימה — 09:00-10:00 ו-11:00-12:00
        _create_pending(db, time="09:00")
        _create_pending(db, time="11:00")
        # תור חדש ב-10:00 — חלון פנוי בין 10:00 ל-11:00 = 60 דק׳ בלבד
        appt_id = _create_pending(db, time="10:00")
        with self._patch_no_business_block(), self._patch_no_gcal():
            opts = db.compute_duration_options_for_appointment(appt_id)
        # רק 30, 45, 60 צריכים לעבור (כל מה שמעבר ל-60 חופף)
        assert 30 in opts and 45 in opts and 60 in opts
        assert 75 not in opts and 90 not in opts


class TestResolveDuration:
    def test_returns_confirmed_when_set(self, db):
        db.add_service("טיפול", duration_minutes=60)
        appt = {"service": "טיפול", "confirmed_duration_minutes": 90}
        assert db.resolve_appointment_duration_minutes(appt) == 90

    def test_falls_back_to_global_default(self, db):
        """ללא confirmed — נופל לברירת מחדל הגלובלית מ-bot_settings."""
        db.update_appointment_duration_settings(
            step_minutes=15, steps_backward=2, steps_forward=4, default_minutes=45,
        )
        appt = {"service": "טיפול"}
        assert db.resolve_appointment_duration_minutes(appt) == 45

    def test_falls_back_to_60_when_no_settings(self, db):
        """ברירת המחדל ההתחלתית של ההגדרות היא 60 דקות."""
        appt = {"service": "לא קיים"}
        assert db.resolve_appointment_duration_minutes(appt) == 60

    def test_ignores_service_specific_duration(self, db):
        """duration_minutes הפר-שירות לא נקרא יותר — ביטלנו את ההגדרה הפרטנית."""
        db.add_service("ארוך", duration_minutes=180)
        db.update_appointment_duration_settings(
            step_minutes=15, steps_backward=2, steps_forward=4, default_minutes=30,
        )
        appt = {"service": "ארוך"}
        # 180 הפר-שירות מתעלמים, מקבלים 30 הגלובלי
        assert db.resolve_appointment_duration_minutes(appt) == 30


class TestComputeDurationOptionsBatch:
    """גרסת batch של compute_duration_options — תיקון N+1 ב-polling."""

    def _patch_no_business_block(self):
        return patch(
            "business_hours.get_status_for_date",
            return_value={"is_open": True, "open_time": "00:00", "close_time": "23:59"},
        )

    def _patch_no_gcal(self):
        return patch("google_calendar.is_connected", return_value=False)

    def test_batch_returns_dict_per_appt(self, db):
        db.add_service("טיפול", duration_minutes=60)
        a1 = _create_pending(db, time="10:00")
        a2 = _create_pending(db, time="14:00")
        with self._patch_no_business_block(), self._patch_no_gcal():
            pending = db.get_appointments(status="pending")
            results = db.compute_duration_options_for_pending(pending)
        assert set(results.keys()) == {a1, a2}
        # שני התורים רחוקים — שניהם מקבלים את המגוון המלא
        assert 60 in results[a1] and 60 in results[a2]

    def test_batch_handles_empty_input(self, db):
        assert db.compute_duration_options_for_pending([]) == {}

    def test_batch_consistent_with_single(self, db):
        """ה-batch צריך להחזיר אותן תוצאות כמו הקריאה הבודדת."""
        db.add_service("טיפול", duration_minutes=60)
        _create_pending(db, time="09:00")
        _create_pending(db, time="11:00")
        a3 = _create_pending(db, time="10:00")
        with self._patch_no_business_block(), self._patch_no_gcal():
            single = db.compute_duration_options_for_appointment(a3)
            batch = db.compute_duration_options_for_pending(
                [a for a in db.get_appointments() if a["id"] == a3]
            )
        assert batch[a3] == single


class TestGCalBusyClamping:
    """באג: אירועי כל-היום וחוצי-חצות לא חסמו אופציות (start==end==0)."""

    def _patch_business_open(self):
        return patch(
            "business_hours.get_status_for_date",
            return_value={"is_open": True, "open_time": "00:00", "close_time": "23:59"},
        )

    def test_all_day_event_blocks_all_options(self, db):
        """אירוע כל-היום (00:00 → 00:00 למחרת) חוסם הכל."""
        db.add_service("טיפול", duration_minutes=60)
        appt_id = _create_pending(db, time="10:00", date="2099-01-05")
        all_day_busy = [{
            "start": "2099-01-05T00:00:00+02:00",
            "end": "2099-01-06T00:00:00+02:00",
        }]
        with self._patch_business_open(), \
             patch("google_calendar.is_connected", return_value=True), \
             patch("google_calendar.get_busy_slots", return_value=all_day_busy):
            opts = db.compute_duration_options_for_appointment(appt_id)
        # כל המועמדים חופפים — נופל ל-default fallback (אופציה אחת)
        assert opts == [60]

    def test_cross_midnight_event_blocks_overlapping_options(self, db):
        """אירוע 23:00 → 02:00 למחרת — slot של 30 דק' (22:30-23:00) עדיין
        תקין כי הוא מסתיים בדיוק כשהאירוע מתחיל; אבל 45 דק' ומעלה חופפים."""
        db.add_service("טיפול", duration_minutes=60)
        appt_id = _create_pending(db, time="22:30", date="2099-01-05")
        cross_busy = [{
            "start": "2099-01-05T23:00:00+02:00",
            "end": "2099-01-06T02:00:00+02:00",
        }]
        with self._patch_business_open(), \
             patch("google_calendar.is_connected", return_value=True), \
             patch("google_calendar.get_busy_slots", return_value=cross_busy):
            opts = db.compute_duration_options_for_appointment(appt_id)
        # 30 דק' = 22:30-23:00 צמוד לתחילת ה-busy → תקין
        assert 30 in opts
        # כל אורך מ-45 דק' ומעלה חופף עם busy → חסום
        assert 45 not in opts
        assert 60 not in opts
        assert 90 not in opts


class TestAddMinutesToHHMM:
    """באג: גלישה ליום הבא הציגה '23:00–00:30' שנראה לא תקין."""

    def test_normal_addition(self):
        from appointment_notifications import _add_minutes_to_hhmm
        assert _add_minutes_to_hhmm("10:00", 90) == "11:30"

    def test_wraps_to_next_day_with_marker(self):
        from appointment_notifications import _add_minutes_to_hhmm
        # 23:00 + 90 = 24:30 → 00:30 למחרת
        result = _add_minutes_to_hhmm("23:00", 90)
        assert "00:30" in result
        assert "למחרת" in result

    def test_exactly_midnight(self):
        from appointment_notifications import _add_minutes_to_hhmm
        # 22:00 + 120 = 24:00 → 00:00 למחרת
        result = _add_minutes_to_hhmm("22:00", 120)
        assert "00:00" in result
        assert "למחרת" in result

    def test_no_wrap_at_23_00(self):
        from appointment_notifications import _add_minutes_to_hhmm
        # 22:30 + 30 = 23:00 — לא גולש
        assert _add_minutes_to_hhmm("22:30", 30) == "23:00"
