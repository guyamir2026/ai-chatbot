"""
טסטים לתזכורות תורים אוטומטיות.
"""

import os
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake-token")
os.environ.setdefault("OPENAI_API_KEY", "fake-key")

from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo

import pytest

ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")


@pytest.fixture
def db(tmp_path):
    """מאתחל DB בקובץ זמני ומחזיר את מודול database מוכן לשימוש."""
    db_path = tmp_path / "test.db"
    with patch("ai_chatbot.config.DB_PATH", db_path):
        import importlib
        import database
        importlib.reload(database)
        database.init_db()
        yield database


class TestSendAppointmentReminders:
    """טסטים ל-send_appointment_reminders — הלוגיקה המרכזית."""

    def _setup_confirmed_appointment(self, db, date, time="10:00"):
        """יצירת תור מאושר לצורך טסט."""
        appt_id = db.create_appointment("u1", "ישראל", service="תספורת",
                                         preferred_date=date, preferred_time=time)
        db.update_appointment_status(appt_id, "confirmed")
        return appt_id

    def test_sends_reminder_for_tomorrow(self, db):
        """שולח תזכורת לתור מאושר של מחר."""
        tomorrow = (datetime.now(ISRAEL_TZ) + timedelta(days=1)).strftime("%Y-%m-%d")
        appt_id = self._setup_confirmed_appointment(db, tomorrow)

        # מוודא שהתזכורות מופעלות ושהשעה כבר הגיעה
        db.update_bot_settings("friendly", "", reminder_enabled=True, reminder_time="00:00")

        with patch("appointment_notifications.send_telegram_message", return_value=True) as mock_send:
            from appointment_notifications import send_appointment_reminders
            result = send_appointment_reminders()

        assert result["sent"] == 1
        assert result["failed"] == 0
        assert result["skipped"] is None
        mock_send.assert_called_once()
        # תזכורת סומנה כנשלחה
        assert db.get_appointment(appt_id)["reminder_sent"] == 1

    def test_skips_when_disabled(self, db):
        """לא שולח כשהתזכורות מכובות."""
        tomorrow = (datetime.now(ISRAEL_TZ) + timedelta(days=1)).strftime("%Y-%m-%d")
        self._setup_confirmed_appointment(db, tomorrow)
        db.update_bot_settings("friendly", "", reminder_enabled=False, reminder_time="00:00")

        with patch("appointment_notifications.send_telegram_message") as mock_send:
            from appointment_notifications import send_appointment_reminders
            result = send_appointment_reminders()

        assert result["skipped"] == "disabled"
        mock_send.assert_not_called()

    def test_skips_pending_appointments(self, db):
        """לא שולח תזכורת לתורים בסטטוס pending (לא מאושרים)."""
        tomorrow = (datetime.now(ISRAEL_TZ) + timedelta(days=1)).strftime("%Y-%m-%d")
        db.create_appointment("u1", "ישראל", preferred_date=tomorrow, preferred_time="10:00")
        db.update_bot_settings("friendly", "", reminder_enabled=True, reminder_time="00:00")

        with patch("appointment_notifications.send_telegram_message") as mock_send:
            from appointment_notifications import send_appointment_reminders
            result = send_appointment_reminders()

        assert result["sent"] == 0
        mock_send.assert_not_called()

    def test_skips_already_reminded(self, db):
        """לא שולח תזכורת כפולה."""
        tomorrow = (datetime.now(ISRAEL_TZ) + timedelta(days=1)).strftime("%Y-%m-%d")
        appt_id = self._setup_confirmed_appointment(db, tomorrow)
        db.mark_reminder_sent(appt_id)
        db.update_bot_settings("friendly", "", reminder_enabled=True, reminder_time="00:00")

        with patch("appointment_notifications.send_telegram_message") as mock_send:
            from appointment_notifications import send_appointment_reminders
            result = send_appointment_reminders()

        assert result["sent"] == 0
        mock_send.assert_not_called()

    def test_telegram_failure_doesnt_mark_sent(self, db):
        """כשל בטלגרם — לא מסמן כנשלח כדי שיישלח שוב."""
        tomorrow = (datetime.now(ISRAEL_TZ) + timedelta(days=1)).strftime("%Y-%m-%d")
        appt_id = self._setup_confirmed_appointment(db, tomorrow)
        db.update_bot_settings("friendly", "", reminder_enabled=True, reminder_time="00:00")

        with patch("appointment_notifications.send_telegram_message", return_value=False):
            from appointment_notifications import send_appointment_reminders
            result = send_appointment_reminders()

        assert result["sent"] == 0
        assert result["failed"] == 1
        # לא סומן — ינסה שוב בריצה הבאה
        assert db.get_appointment(appt_id)["reminder_sent"] == 0


class TestSendSecondReminders:
    """טסטים ל-send_second_reminders — תזכורת שעתיים לפני התור."""

    def _setup_confirmed_appointment(self, db, date, time="10:00"):
        """יצירת תור מאושר לצורך טסט."""
        appt_id = db.create_appointment("u1", "ישראל", service="תספורת",
                                         preferred_date=date, preferred_time=time)
        db.update_appointment_status(appt_id, "confirmed")
        return appt_id

    def _make_fake_now(self, year, month, day, hour, minute=0):
        """יצירת datetime מזויף עם patch-friendly wrapping."""
        return datetime(year, month, day, hour, minute, tzinfo=ISRAEL_TZ)

    def test_sends_second_reminder_in_window(self, db):
        """שולח תזכורת שנייה לתור שבעוד שעתיים."""
        fake_now = self._make_fake_now(2026, 4, 1, 8, 0)
        target_date = "2026-04-01"
        appt_id = self._setup_confirmed_appointment(db, target_date, "10:15")
        db.update_bot_settings("friendly", "", second_reminder_enabled=True)

        import appointment_notifications as an_mod
        original_datetime = datetime

        class FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fake_now

        with patch("appointment_notifications.send_telegram_message", return_value=True) as mock_send, \
             patch.object(an_mod, "datetime", FakeDatetime):
            result = an_mod.send_second_reminders()

        assert result["sent"] == 1
        assert result["failed"] == 0
        mock_send.assert_called_once()
        assert db.get_appointment(appt_id)["second_reminder_sent"] == 1

    def test_skips_when_disabled(self, db):
        """לא שולח תזכורת שנייה כשהאפשרות מכובה."""
        target_date = "2026-04-01"
        self._setup_confirmed_appointment(db, target_date, "10:15")
        db.update_bot_settings("friendly", "", second_reminder_enabled=False)

        with patch("appointment_notifications.send_telegram_message") as mock_send:
            from appointment_notifications import send_second_reminders
            result = send_second_reminders()

        assert result["skipped"] == "disabled"
        mock_send.assert_not_called()

    def test_skips_outside_window(self, db):
        """לא שולח תזכורת לתור שלא בחלון השעתיים."""
        fake_now = self._make_fake_now(2026, 4, 1, 8, 0)
        target_date = "2026-04-01"
        self._setup_confirmed_appointment(db, target_date, "14:00")
        db.update_bot_settings("friendly", "", second_reminder_enabled=True)

        import appointment_notifications as an_mod

        class FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fake_now

        with patch("appointment_notifications.send_telegram_message") as mock_send, \
             patch.object(an_mod, "datetime", FakeDatetime):
            result = an_mod.send_second_reminders()

        assert result["sent"] == 0
        mock_send.assert_not_called()

    def test_custom_hours_before(self, db):
        """שולח תזכורת לפי מספר שעות מוגדר (3 שעות)."""
        # "עכשיו" 07:00 + 3 שעות → חלון 10:00–10:30
        fake_now = self._make_fake_now(2026, 4, 1, 7, 0)
        target_date = "2026-04-01"
        appt_id = self._setup_confirmed_appointment(db, target_date, "10:15")
        db.update_bot_settings("friendly", "", second_reminder_enabled=True, second_reminder_hours=3.0)

        import appointment_notifications as an_mod

        class FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fake_now

        with patch("appointment_notifications.send_telegram_message", return_value=True) as mock_send, \
             patch.object(an_mod, "datetime", FakeDatetime):
            result = an_mod.send_second_reminders()

        assert result["sent"] == 1
        # מוודא שהודעת התזכורת מכילה "3 שעות"
        sent_text = mock_send.call_args[0][1]
        assert "3 שעות" in sent_text
        assert db.get_appointment(appt_id)["second_reminder_sent"] == 1

    def test_skips_already_reminded(self, db):
        """לא שולח תזכורת שנייה כפולה."""
        fake_now = self._make_fake_now(2026, 4, 1, 8, 0)
        target_date = "2026-04-01"
        appt_id = self._setup_confirmed_appointment(db, target_date, "10:15")
        db.mark_second_reminder_sent(appt_id)
        db.update_bot_settings("friendly", "", second_reminder_enabled=True)

        import appointment_notifications as an_mod

        class FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fake_now

        with patch("appointment_notifications.send_telegram_message") as mock_send, \
             patch.object(an_mod, "datetime", FakeDatetime):
            result = an_mod.send_second_reminders()

        assert result["sent"] == 0
        mock_send.assert_not_called()

    def test_midnight_crossing_window(self, db):
        """שולח תזכורת גם כשהחלון חוצה חצות."""
        # "עכשיו" 21:45 + 2 שעות → חלון 23:45–00:15, תור ב-00:05 ביום המחרת
        fake_now = self._make_fake_now(2026, 4, 1, 21, 45)
        next_date = "2026-04-02"
        appt_id = self._setup_confirmed_appointment(db, next_date, "00:05")
        db.update_bot_settings("friendly", "", second_reminder_enabled=True, second_reminder_hours=2.0)

        import appointment_notifications as an_mod

        class FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fake_now

        with patch("appointment_notifications.send_telegram_message", return_value=True) as mock_send, \
             patch.object(an_mod, "datetime", FakeDatetime):
            result = an_mod.send_second_reminders()

        assert result["sent"] == 1
        assert db.get_appointment(appt_id)["second_reminder_sent"] == 1
