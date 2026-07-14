"""
טסטים לשליחת קובץ ICS אחרי אישור תור — Telegram + WhatsApp.

מכסים:
- whatsapp_sender.send_whatsapp עם media_url — Twilio מקבל את הפרמטר
- /ics/<page_id> — Content-Type, Content-Disposition, 404 על חסר
- _send_ics_file ב-WhatsApp — יצירת page_id, URL נכון, send_whatsapp נקרא
- _send_ics_file ב-WhatsApp ללא ADMIN_URL — דילוג גרציוזי
- _send_ics_file ב-Telegram — שולח דרך send_telegram_document (רגרסיה)
"""

import importlib
import sys
import types
from unittest.mock import MagicMock, patch

import pytest


# ── Mock twilio ──────────────────────────────────────────────────────────────


_twilio_mock = types.ModuleType("twilio")
_twilio_rest = types.ModuleType("twilio.rest")
_twilio_rest.Client = MagicMock()
sys.modules.setdefault("twilio", _twilio_mock)
sys.modules.setdefault("twilio.rest", _twilio_rest)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    with patch("ai_chatbot.config.DB_PATH", db_path):
        import database
        importlib.reload(database)
        database.init_db()
        yield database


# ── whatsapp_sender.send_whatsapp(media_url) ─────────────────────────────────


class TestSendWhatsAppMedia:
    def test_media_url_passed_to_twilio(self, monkeypatch):
        """media_url מועבר ל-client.messages.create כרשימה."""
        monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC" + "a" * 32)
        monkeypatch.setenv("TWILIO_AUTH_TOKEN", "b" * 32)
        monkeypatch.setenv("TWILIO_WHATSAPP_NUMBER", "+14155551234")

        # reset singleton client
        from messaging import whatsapp_sender
        whatsapp_sender._twilio_client = None

        mock_client = MagicMock()
        mock_client.messages.create = MagicMock()

        with patch.object(whatsapp_sender, "_get_twilio_client", return_value=mock_client):
            whatsapp_sender.send_whatsapp(
                "+972501234567", "טקסט קצר", media_url="https://example.com/file.ics",
            )

        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs["media_url"] == ["https://example.com/file.ics"]
        assert call_kwargs["to"] == "whatsapp:+972501234567"

    def test_no_media_url_means_no_kwarg(self, monkeypatch):
        """ללא media_url → ה-kwarg לא מועבר (לא None) — Twilio לא מצפה לו."""
        monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC" + "a" * 32)
        monkeypatch.setenv("TWILIO_AUTH_TOKEN", "b" * 32)
        monkeypatch.setenv("TWILIO_WHATSAPP_NUMBER", "+14155551234")

        from messaging import whatsapp_sender
        whatsapp_sender._twilio_client = None

        mock_client = MagicMock()
        mock_client.messages.create = MagicMock()

        with patch.object(whatsapp_sender, "_get_twilio_client", return_value=mock_client):
            whatsapp_sender.send_whatsapp("+972501234567", "טקסט")

        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert "media_url" not in call_kwargs


# ── /ics/<page_id> route ─────────────────────────────────────────────────────


@pytest.fixture
def admin_client(tmp_path, monkeypatch):
    """Flask test client בלי login — /ics ציבורי."""
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "testpass123")
    monkeypatch.setenv("ADMIN_SECRET_KEY", "test-secret-ics")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))

    import config as _root_config
    importlib.reload(_root_config)
    import ai_chatbot.config
    importlib.reload(ai_chatbot.config)
    import database
    importlib.reload(database)
    database.init_db()
    import admin.app as _admin_app
    importlib.reload(_admin_app)

    from admin.app import create_admin_app
    app = _admin_app.create_admin_app() if False else create_admin_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False

    with app.test_client() as client:
        yield client, database


class TestICSRoute:
    def test_serves_ics_with_correct_headers(self, admin_client):
        client, db = admin_client
        ics_text = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n"
        page_id = db.create_response_page(content=ics_text, title="appointment_2026-05-01")

        resp = client.get(f"/ics/{page_id}")
        assert resp.status_code == 200
        assert resp.headers["Content-Type"].startswith("text/calendar")
        assert 'attachment' in resp.headers["Content-Disposition"]
        assert 'appointment_2026-05-01.ics' in resp.headers["Content-Disposition"]
        assert b"BEGIN:VCALENDAR" in resp.data

    def test_404_for_missing_page(self, admin_client):
        client, _ = admin_client
        resp = client.get("/ics/nonexistent")
        assert resp.status_code == 404


# ── _send_ics_file: WhatsApp + Telegram paths ────────────────────────────────


class TestSendIcsFile:
    def _make_appt(self):
        return {
            "id": 7,
            "user_id": "+972501234567",
            "service": "תספורת",
            "preferred_date": "2026-05-01",
            "preferred_time": "10:00",
        }

    def test_whatsapp_creates_page_and_sends_link_in_body(self, db, monkeypatch):
        """WhatsApp: ICS נשמר ב-response_pages, URL נשלח כקישור בטקסט (לא media_url).

        רגרסיה לבאג שבו WhatsApp לא תומך ב-text/calendar כקובץ media —
        Twilio שולחת את ה-media_url אבל WhatsApp משמיט את הקובץ
        והלקוח רואה רק טקסט בלי משהו ללחוץ עליו.
        """
        monkeypatch.setenv("ADMIN_URL", "https://admin.example.com")
        # ai_chatbot.config הוא wrapper שעושה `from config import *` — חייב
        # לטעון מחדש את שניהם כדי שהקריאה הפנימית תקבל את הערך החדש.
        import importlib as _imp
        import config as _root_config
        _imp.reload(_root_config)
        import ai_chatbot.config
        _imp.reload(ai_chatbot.config)
        import appointment_notifications
        _imp.reload(appointment_notifications)

        sent = {}

        def fake_send(user_id, text, media_url=None):
            sent["user_id"] = user_id
            sent["text"] = text
            sent["media_url"] = media_url

        with patch("appointment_notifications.db", db), \
             patch("messaging.whatsapp_sender.send_whatsapp", side_effect=fake_send):
            appointment_notifications._send_ics_file(self._make_appt(), channel="whatsapp")

        # send_whatsapp נקרא בלי media_url — ה-URL בגוף ההודעה
        assert sent["user_id"] == "+972501234567"
        assert sent["media_url"] is None
        assert "https://admin.example.com/ics/" in sent["text"]
        assert "ליומן" in sent["text"]

        # נשמרה רשומה ב-response_pages עם תוכן ICS
        page_id = sent["text"].rsplit("/", 1)[-1].strip()
        page = db.get_response_page(page_id)
        assert page is not None
        assert "BEGIN:VCALENDAR" in page["content"]
        assert page["title"] == "appointment_2026-05-01"

    def test_whatsapp_skipped_when_admin_url_missing(self, db, monkeypatch):
        """ללא ADMIN_URL — לא ניתן לבנות URL ציבורי ל-Twilio, מדלגים בשקט."""
        monkeypatch.setenv("ADMIN_URL", "")
        import importlib as _imp
        import config as _root_config
        _imp.reload(_root_config)
        import ai_chatbot.config
        _imp.reload(ai_chatbot.config)
        import appointment_notifications
        _imp.reload(appointment_notifications)

        called = []
        with patch("appointment_notifications.db", db), \
             patch("messaging.whatsapp_sender.send_whatsapp",
                   side_effect=lambda *a, **k: called.append((a, k))):
            appointment_notifications._send_ics_file(self._make_appt(), channel="whatsapp")

        assert called == []
        # גם לא נוצרה רשומת page (אין לאן להפנות)
        with db.get_connection() as conn:
            count = conn.execute("SELECT COUNT(*) AS c FROM response_pages").fetchone()["c"]
        assert count == 0

    def test_telegram_uses_send_telegram_document(self, db):
        """Telegram regression — נשלח כמסמך עם file_data בייטים."""
        import appointment_notifications
        sent = {}

        def fake_send_doc(chat_id, file_data, filename, caption):
            sent["chat_id"] = chat_id
            sent["filename"] = filename
            sent["bytes_prefix"] = file_data[:13]  # "BEGIN:VCALEND"
            return True

        with patch("appointment_notifications.db", db), \
             patch("appointment_notifications.send_telegram_document",
                   side_effect=fake_send_doc):
            appointment_notifications._send_ics_file(self._make_appt(), channel="telegram")

        assert sent["chat_id"] == "+972501234567"
        assert sent["filename"] == "appointment_2026-05-01.ics"
        assert sent["bytes_prefix"] == b"BEGIN:VCALEND"

    def test_disabled_in_settings_skips_both_channels(self, db, monkeypatch):
        """ics_enabled=0 → לא נשלח כלום, אף שלא נכשלים."""
        db.update_bot_settings(tone="friendly", ics_enabled=0)
        import appointment_notifications

        with patch("appointment_notifications.db", db), \
             patch("messaging.whatsapp_sender.send_whatsapp") as wa, \
             patch("appointment_notifications.send_telegram_document") as tg:
            appointment_notifications._send_ics_file(self._make_appt(), channel="whatsapp")
            appointment_notifications._send_ics_file(self._make_appt(), channel="telegram")

        wa.assert_not_called()
        tg.assert_not_called()
