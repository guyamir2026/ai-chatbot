"""
טסטים ל-/qr-code — תמיכה בשני ערוצים (Telegram + WhatsApp).

מכסים:
- preview/download עם channel=telegram (רגרסיה)
- preview/download עם channel=whatsapp (חדש) — בונה wa.me/{digits}
- channel לא מוגדר → 404 ב-preview, redirect+flash ב-download
- to_wa_me_digits מנקה תווים שאינם ספרות
- הדף עצמו מציג שתי האפשרויות כששניהם מוגדרים
"""

import importlib
from unittest.mock import patch

import pytest


@pytest.fixture
def admin_client(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "testpass123")
    monkeypatch.setenv("ADMIN_SECRET_KEY", "test-secret-qr")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "MyTestBot")
    monkeypatch.setenv("TWILIO_WHATSAPP_NUMBER", "+14155551234")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "")

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
    app = create_admin_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False

    with app.test_client() as client:
        client.post("/login", data={
            "username": "admin", "password": "testpass123",
        })
        yield client


# ── to_wa_me_digits util ─────────────────────────────────────────────────────


class TestToWaMeDigits:
    def test_strips_plus_and_separators(self):
        from utils.phone import to_wa_me_digits
        assert to_wa_me_digits("+972 50-123-4567") == "972501234567"

    def test_already_digits(self):
        from utils.phone import to_wa_me_digits
        assert to_wa_me_digits("972501234567") == "972501234567"

    def test_empty(self):
        from utils.phone import to_wa_me_digits
        assert to_wa_me_digits("") == ""
        assert to_wa_me_digits(None) == ""


# ── /qr-code page ────────────────────────────────────────────────────────────


class TestQRPage:
    def test_page_lists_both_channels_when_both_configured(self, admin_client):
        resp = admin_client.get("/qr-code")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        # שני הסלקטורים מופיעים
        assert 'value="telegram"' in body
        assert 'value="whatsapp"' in body
        # קישור wa.me נבנה עם digits בלבד (בלי + ובלי רווחים)
        assert "wa.me/14155551234" in body
        assert "telegram.me/MyTestBot" in body

    def test_link_uses_same_digits_as_qr_for_messy_number(
        self, tmp_path, monkeypatch,
    ):
        """מספר עם סוגריים/נקודות → הקישור wa.me תואם בדיוק ל-URL שב-QR.

        רגרסיה: ה-template ניקה רק +/רווח/- ב-replace; to_wa_me_digits בצד
        השרת מנקה כל תו שאינו ספרה. ערכים כמו "+1 (415) 555.1234" יצרו
        אי-התאמה בין הקישור הקליקבילי לבין ה-URL שמקודד ב-QR.
        """
        monkeypatch.setenv("ADMIN_USERNAME", "admin")
        monkeypatch.setenv("ADMIN_PASSWORD", "testpass123")
        monkeypatch.setenv("ADMIN_SECRET_KEY", "test-secret-qr2")
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
        monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "")
        monkeypatch.setenv("TWILIO_WHATSAPP_NUMBER", "+1 (415) 555.1234")
        monkeypatch.setenv("TWILIO_ACCOUNT_SID", "")
        monkeypatch.setenv("TWILIO_AUTH_TOKEN", "")

        import config as _root_config; importlib.reload(_root_config)
        import ai_chatbot.config; importlib.reload(ai_chatbot.config)
        import database; importlib.reload(database); database.init_db()
        import admin.app as _admin_app; importlib.reload(_admin_app)

        from admin.app import create_admin_app
        app = create_admin_app()
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        with app.test_client() as c:
            c.post("/login", data={"username": "admin", "password": "testpass123"})
            resp = c.get("/qr-code")
            assert resp.status_code == 200
            body = resp.data.decode("utf-8")
            # הקישור הקליקבילי משתמש ב-digits בלבד — בלי סוגריים/נקודות
            assert "wa.me/14155551234" in body
            assert "wa.me/(415)" not in body
            assert "555.1234" not in body


# ── /qr-code/preview ─────────────────────────────────────────────────────────


class TestQRPreview:
    def test_telegram_default(self, admin_client):
        """ברירת מחדל = telegram (רגרסיה)."""
        resp = admin_client.get("/qr-code/preview")
        assert resp.status_code == 200
        assert resp.mimetype == "image/png"
        # PNG magic bytes
        assert resp.data[:8] == b"\x89PNG\r\n\x1a\n"

    def test_whatsapp_channel(self, admin_client):
        resp = admin_client.get("/qr-code/preview?channel=whatsapp")
        assert resp.status_code == 200
        assert resp.mimetype == "image/png"
        assert resp.data[:8] == b"\x89PNG\r\n\x1a\n"

    def test_unknown_channel_falls_back_to_telegram(self, admin_client):
        """ערוץ לא מוכר → telegram (לא 500)."""
        resp = admin_client.get("/qr-code/preview?channel=fake")
        assert resp.status_code == 200


class TestQRPreviewMissingConfig:
    """ערוץ לא מוגדר → 404 ב-preview."""

    @pytest.fixture
    def client_no_telegram(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ADMIN_USERNAME", "admin")
        monkeypatch.setenv("ADMIN_PASSWORD", "testpass123")
        monkeypatch.setenv("ADMIN_SECRET_KEY", "test-secret-qr")
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
        monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "")  # רק WhatsApp
        monkeypatch.setenv("TWILIO_WHATSAPP_NUMBER", "+14155551234")
        monkeypatch.setenv("TWILIO_ACCOUNT_SID", "")
        monkeypatch.setenv("TWILIO_AUTH_TOKEN", "")

        import config as _root_config; importlib.reload(_root_config)
        import ai_chatbot.config; importlib.reload(ai_chatbot.config)
        import database; importlib.reload(database); database.init_db()
        import admin.app as _admin_app; importlib.reload(_admin_app)

        from admin.app import create_admin_app
        app = create_admin_app()
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        with app.test_client() as c:
            c.post("/login", data={"username": "admin", "password": "testpass123"})
            yield c

    def test_telegram_preview_when_only_whatsapp(self, client_no_telegram):
        """אין TELEGRAM_BOT_USERNAME → preview של telegram מחזיר 404."""
        resp = client_no_telegram.get("/qr-code/preview?channel=telegram")
        assert resp.status_code == 404

    def test_whatsapp_preview_when_only_whatsapp(self, client_no_telegram):
        """WhatsApp עובד גם כשטלגרם לא מוגדר."""
        resp = client_no_telegram.get("/qr-code/preview?channel=whatsapp")
        assert resp.status_code == 200

    def test_whatsapp_link_visible_when_only_whatsapp(self, client_no_telegram):
        """כשרק WhatsApp מוגדר — הקישור מתחת ל-QR מופיע (לא display:none).

        רגרסיה: היה display:none קשיח על qr-link-whatsapp; ללא רדיו טלגרם,
        updateChannel לעולם לא קרא ולכן הקישור היה נשאר נסתר.
        """
        resp = client_no_telegram.get("/qr-code")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        # החילוץ של הקישור — חייב להיות display:inline
        # מחפשים את הבלוק של qr-link-whatsapp בלבד
        idx = body.find('id="qr-link-whatsapp"')
        assert idx != -1
        snippet = body[idx:idx + 400]
        assert "display: inline" in snippet
        # והקישור לטלגרם דווקא נסתר (אין username)
        idx_tg = body.find('id="qr-link-telegram"')
        assert idx_tg != -1
        snippet_tg = body[idx_tg:idx_tg + 400]
        assert "display: none" in snippet_tg


# ── /qr-code/download ────────────────────────────────────────────────────────


class TestQRDownload:
    def test_telegram_filename_includes_username(self, admin_client):
        resp = admin_client.get("/qr-code/download?channel=telegram")
        assert resp.status_code == 200
        cd = resp.headers["Content-Disposition"]
        assert "qr_MyTestBot.png" in cd

    def test_whatsapp_filename_includes_digits(self, admin_client):
        resp = admin_client.get("/qr-code/download?channel=whatsapp")
        assert resp.status_code == 200
        cd = resp.headers["Content-Disposition"]
        assert "qr_whatsapp_14155551234.png" in cd

    def test_missing_channel_redirects_with_flash(self, admin_client, monkeypatch):
        """ערוץ לא מוגדר → redirect לעמוד ה-QR (לא 500)."""
        # ניקוי TWILIO_WHATSAPP_NUMBER בלייב — ה-route קורא דינמית מ-_cfg
        import ai_chatbot.config as _cfg
        monkeypatch.setattr(_cfg, "TWILIO_WHATSAPP_NUMBER", "")
        resp = admin_client.get("/qr-code/download?channel=whatsapp",
                                follow_redirects=False)
        assert resp.status_code == 302
        assert "/qr-code" in resp.location
