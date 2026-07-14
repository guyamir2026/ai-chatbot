"""
טסטים לנעילת ערוץ ב-/bot-config לפי החבילה.

כללים:
- חבילת basic (Telegram) → קלפי WhatsApp נעולים בתבנית, וה-route
  מחזיר 302 + flash שגיאה אם מנסים לעדכן ב-curl/HTMX.
- חבילת advanced/premium (WhatsApp) → אותו דין הפוך לטלגרם.
"""

import importlib

import pytest


def _build_app(monkeypatch, tmp_path, *, plan: str = "basic"):
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "adminpass123")
    monkeypatch.setenv("ADMIN_SECRET_KEY", "test-secret")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DEVELOPER_PASSWORD", "")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "")
    monkeypatch.setenv("TWILIO_WHATSAPP_NUMBER", "")

    import config as _root_config
    importlib.reload(_root_config)
    import ai_chatbot.config
    importlib.reload(ai_chatbot.config)
    import database
    importlib.reload(database)
    database.init_db()
    import feature_flags
    importlib.reload(feature_flags)
    # ברירת המחדל היא premium; קובעים מפורשות (כולל basic).
    feature_flags.set_plan(plan, reason="test")
    import admin.app as _admin_app
    importlib.reload(_admin_app)

    from admin.app import create_admin_app
    app = create_admin_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    return app.test_client()


def _login(client):
    client.post("/login", data={"username": "admin", "password": "adminpass123"})


# ── תבנית: HTML של הטופס נעול לפי החבילה ─────────────────────────────────


class TestBotConfigUILocking:
    def test_basic_locks_whatsapp_form(self, tmp_path, monkeypatch):
        client = _build_app(monkeypatch, tmp_path, plan="basic")
        _login(client)
        body = client.get("/bot-config").data.decode("utf-8")
        # קלף WhatsApp מסומן כנעול ב-class
        assert "channel-card-disabled" in body
        # שדות WhatsApp מקבלים disabled
        assert 'name="twilio_account_sid"' in body
        # נמצא את החלק הספציפי של ה-input ונבדוק disabled
        # (ב-Jinja זה רק תכונה — אם {% if disabled %}disabled{% endif %} → disabled נכתב)
        # נחפש קטע שמציין disabled על twilio_account_sid
        idx = body.find('id="twilio_account_sid"')
        assert idx > 0
        # disabled חייב להופיע בתוך אותו tag <input>
        # נחפש את ה-> שסוגר את ה-tag
        end = body.find(">", idx)
        input_tag = body[idx:end]
        assert "disabled" in input_tag

    def test_basic_does_not_lock_telegram_form(self, tmp_path, monkeypatch):
        client = _build_app(monkeypatch, tmp_path, plan="basic")
        _login(client)
        body = client.get("/bot-config").data.decode("utf-8")
        # שדה Telegram לא אמור להיות disabled ב-basic
        idx = body.find('id="telegram_bot_token"')
        end = body.find(">", idx)
        input_tag = body[idx:end]
        assert "disabled" not in input_tag

    def test_premium_locks_telegram_form(self, tmp_path, monkeypatch):
        client = _build_app(monkeypatch, tmp_path, plan="premium")
        _login(client)
        body = client.get("/bot-config").data.decode("utf-8")
        # ב-premium (WhatsApp) → קלף Telegram נעול
        idx = body.find('id="telegram_bot_token"')
        end = body.find(">", idx)
        input_tag = body[idx:end]
        assert "disabled" in input_tag

    def test_premium_does_not_lock_whatsapp_form(self, tmp_path, monkeypatch):
        client = _build_app(monkeypatch, tmp_path, plan="premium")
        _login(client)
        body = client.get("/bot-config").data.decode("utf-8")
        # שדה WhatsApp פעיל
        idx = body.find('id="twilio_account_sid"')
        end = body.find(">", idx)
        input_tag = body[idx:end]
        assert "disabled" not in input_tag

    def test_advanced_uses_whatsapp_too(self, tmp_path, monkeypatch):
        # advanced גם הוא WhatsApp → טלגרם נעול
        client = _build_app(monkeypatch, tmp_path, plan="advanced")
        _login(client)
        body = client.get("/bot-config").data.decode("utf-8")
        idx = body.find('id="telegram_bot_token"')
        end = body.find(">", idx)
        input_tag = body[idx:end]
        assert "disabled" in input_tag


# ── Backend: לא ניתן לעדכן את הערוץ הנעול גם ב-curl ────────────────────


class TestBotConfigBackendLocking:
    def test_basic_blocks_whatsapp_post(self, tmp_path, monkeypatch):
        client = _build_app(monkeypatch, tmp_path, plan="basic")
        _login(client)
        resp = client.post(
            "/bot-config",
            data={
                "form_type": "whatsapp",
                "twilio_account_sid": "AC" + "a" * 32,
                "twilio_auth_token": "b" * 32,
                "twilio_whatsapp_number": "+14155551234",
            },
            follow_redirects=False,
        )
        # redirect חזרה (302) עם flash שגיאה — לא 500, לא 200 OK
        assert resp.status_code == 302
        # ודאו שהערך לא נשמר ל-env (לא נכתב בפועל)
        import ai_chatbot.config as _cfg
        assert _cfg.TWILIO_ACCOUNT_SID == ""
        assert _cfg.TWILIO_AUTH_TOKEN == ""

    def test_basic_allows_telegram_post(self, tmp_path, monkeypatch):
        client = _build_app(monkeypatch, tmp_path, plan="basic")
        _login(client)
        resp = client.post(
            "/bot-config",
            data={
                "form_type": "telegram",
                "telegram_bot_token": "test-new-token",
                "telegram_owner_chat_id": "987654321",
            },
            follow_redirects=False,
        )
        # ב-basic (Telegram) → טופס Telegram מתקבל ונשמר
        assert resp.status_code == 302
        import ai_chatbot.config as _cfg
        assert _cfg.TELEGRAM_BOT_TOKEN == "test-new-token"

    def test_premium_blocks_telegram_post(self, tmp_path, monkeypatch):
        client = _build_app(monkeypatch, tmp_path, plan="premium")
        _login(client)
        resp = client.post(
            "/bot-config",
            data={
                "form_type": "telegram",
                "telegram_bot_token": "tampered-token",
                "telegram_owner_chat_id": "111",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302
        import ai_chatbot.config as _cfg
        # הטוקן לא הוחלף
        assert _cfg.TELEGRAM_BOT_TOKEN != "tampered-token"

    def test_premium_allows_whatsapp_post(self, tmp_path, monkeypatch):
        client = _build_app(monkeypatch, tmp_path, plan="premium")
        _login(client)
        resp = client.post(
            "/bot-config",
            data={
                "form_type": "whatsapp",
                "twilio_account_sid": "AC" + "f" * 32,
                "twilio_auth_token": "e" * 32,
                "twilio_whatsapp_number": "+14155559999",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302
        import ai_chatbot.config as _cfg
        assert _cfg.TWILIO_ACCOUNT_SID == "AC" + "f" * 32
