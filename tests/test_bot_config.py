"""
טסטים לטופס הגדרות תשתית בפאנל (/bot-config).

מתמקדים בענף WhatsApp/Twilio שנוסף — ולידציה, write-only לסודות,
ושמירה ל-.env, os.environ ו-_cfg.
"""

import importlib
from unittest.mock import patch

import pytest


@pytest.fixture
def admin_client(tmp_path, monkeypatch):
    """Flask test client עם login + DATA_DIR זמני (לקובץ .env שייכתב)."""
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "testpass123")
    monkeypatch.setenv("ADMIN_SECRET_KEY", "test-secret-key-for-bot-config")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    # ה-handler כותב os.environ ישירות — חייבים monkeypatch כדי שהקריאות
    # יתאוששו אחרי הטסט (אחרת מזהמים את validate_config בטסטים אחרים).
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
    # ב-Phase 4 הוספה נעילת ערוץ ב-/bot-config — חבילת basic חוסמת
    # עדכוני WhatsApp. הטסטים פה בודקים את לוגיקת ה-WhatsApp עצמה,
    # לא את ה-gate. לכן מגדירים premium (Whatsapp channel) דרך feature_flags.
    import feature_flags
    importlib.reload(feature_flags)
    feature_flags.set_plan("premium", reason="test fixture")
    import admin.app as _admin_app
    importlib.reload(_admin_app)

    from admin.app import create_admin_app
    app = create_admin_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False

    with app.test_client() as client:
        client.post("/login", data={
            "username": "admin",
            "password": "testpass123",
        })
        yield client


def _read_env(env_path) -> dict:
    """קריאת .env פשוטה — KEY=VALUE לכל שורה."""
    if not env_path.exists():
        return {}
    out = {}
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("'").strip('"')
    return out


# ── WhatsApp form — happy path ───────────────────────────────────────────────


class TestBotConfigWhatsApp:
    def test_get_renders_whatsapp_section(self, admin_client):
        """דף הגדרות תשתית כולל סקציית WhatsApp."""
        resp = admin_client.get("/bot-config")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert "twilio_account_sid" in body
        assert "twilio_auth_token" in body
        assert "twilio_whatsapp_number" in body
        # סודות לא נחשפים בערך השדה — רק "מוגדר/לא מוגדר"
        assert 'name="twilio_account_sid" value=""' in body
        assert 'name="twilio_auth_token" value=""' in body

    def test_save_valid_credentials(self, admin_client, tmp_path):
        """שמירה תקינה של SID + Auth Token + מספר → נכתב ל-.env ו-os.environ."""
        sid = "AC" + "a" * 32
        token = "b" * 32
        number = "+14155551234"

        resp = admin_client.post("/bot-config", data={
            "form_type": "whatsapp",
            "twilio_account_sid": sid,
            "twilio_auth_token": token,
            "twilio_whatsapp_number": number,
        }, follow_redirects=False)

        assert resp.status_code == 302
        env = _read_env(tmp_path / ".env")
        assert env["TWILIO_ACCOUNT_SID"] == sid
        assert env["TWILIO_AUTH_TOKEN"] == token
        assert env["TWILIO_WHATSAPP_NUMBER"] == number

        import os
        assert os.environ["TWILIO_ACCOUNT_SID"] == sid
        assert os.environ["TWILIO_AUTH_TOKEN"] == token
        assert os.environ["TWILIO_WHATSAPP_NUMBER"] == number

    def test_empty_secrets_dont_overwrite(self, admin_client, tmp_path):
        """write-only — שדה ריק ל-SID/Token לא דורס ערך קיים. מספר ריק מנקה (מכוון)."""
        # שלב 1: שמירת ערכים תקפים
        sid = "AC" + "c" * 32
        token = "d" * 32
        admin_client.post("/bot-config", data={
            "form_type": "whatsapp",
            "twilio_account_sid": sid,
            "twilio_auth_token": token,
            "twilio_whatsapp_number": "+14155551234",
        })

        # שלב 2: submit בלי SID/Token — אסור שיתאפסו
        admin_client.post("/bot-config", data={
            "form_type": "whatsapp",
            "twilio_account_sid": "",
            "twilio_auth_token": "",
            "twilio_whatsapp_number": "+14155551234",
        })

        env = _read_env(tmp_path / ".env")
        assert env["TWILIO_ACCOUNT_SID"] == sid
        assert env["TWILIO_AUTH_TOKEN"] == token

    def test_clear_number_writes_empty(self, admin_client, tmp_path):
        """ניקוי מספר WhatsApp = השבתת WhatsApp (מכוון, לא שגיאה)."""
        admin_client.post("/bot-config", data={
            "form_type": "whatsapp",
            "twilio_account_sid": "AC" + "e" * 32,
            "twilio_auth_token": "f" * 32,
            "twilio_whatsapp_number": "+14155551234",
        })
        admin_client.post("/bot-config", data={
            "form_type": "whatsapp",
            "twilio_account_sid": "",
            "twilio_auth_token": "",
            "twilio_whatsapp_number": "",
        })
        env = _read_env(tmp_path / ".env")
        assert env["TWILIO_WHATSAPP_NUMBER"] == ""


# ── Validation errors ────────────────────────────────────────────────────────


class TestBotConfigWhatsAppValidation:
    def test_invalid_sid_rejected(self, admin_client, tmp_path):
        """SID שלא בפורמט AC+32hex → דחיה, .env לא נכתב."""
        resp = admin_client.post("/bot-config", data={
            "form_type": "whatsapp",
            "twilio_account_sid": "not-a-valid-sid",
            "twilio_auth_token": "",
            "twilio_whatsapp_number": "",
        }, follow_redirects=True)
        # הודעת flash בעברית — חיפוש string כולל "Account SID"
        assert b"Account SID" in resp.data
        env = _read_env(tmp_path / ".env")
        assert "TWILIO_ACCOUNT_SID" not in env

    def test_invalid_auth_token_rejected(self, admin_client, tmp_path):
        """Auth Token שלא 32 hex → דחיה."""
        resp = admin_client.post("/bot-config", data={
            "form_type": "whatsapp",
            "twilio_account_sid": "",
            "twilio_auth_token": "short",
            "twilio_whatsapp_number": "",
        }, follow_redirects=True)
        assert "Auth Token".encode("utf-8") in resp.data
        env = _read_env(tmp_path / ".env")
        assert "TWILIO_AUTH_TOKEN" not in env

    def test_invalid_number_rejected(self, admin_client, tmp_path):
        """מספר WhatsApp לא ב-E.164 → דחיה."""
        resp = admin_client.post("/bot-config", data={
            "form_type": "whatsapp",
            "twilio_account_sid": "",
            "twilio_auth_token": "",
            "twilio_whatsapp_number": "0501234567",  # חסר +972
        }, follow_redirects=True)
        # bytes search על העברית של flash
        assert "E.164".encode("utf-8") in resp.data
        env = _read_env(tmp_path / ".env")
        # לא נכתב כלום
        assert "TWILIO_WHATSAPP_NUMBER" not in env

    def test_partial_valid_partial_invalid_aborts_all(self, admin_client, tmp_path):
        """SID תקין + Token לא תקין → אף שדה לא נשמר (atomicity).

        קריטי: ה-SID חייב לעבור ולידציה (AC + 32 hex) כדי שנבדוק באמת
        שהוולידציה של ה-Token מבטלת את הכתיבה של ה-SID. שימוש בערך
        לא-hex ל-SID מפיל את הולידציה הראשונה ומאפס את הטסט.
        """
        valid_sid = "AC" + "a" * 32  # SID תקין — h-a הוא hex
        resp = admin_client.post("/bot-config", data={
            "form_type": "whatsapp",
            "twilio_account_sid": valid_sid,
            "twilio_auth_token": "not-32-hex-chars",  # Token לא תקין
            "twilio_whatsapp_number": "",
        }, follow_redirects=True)
        # הודעת flash על Token (ולא רק לייבל הטופס) — מוודאים בעקיפין
        # ע"י כך שה-SID לא נשמר, וגם בודקים שיש flash בעברית
        assert "Auth Token".encode("utf-8") in resp.data
        env = _read_env(tmp_path / ".env")
        # ה-SID התקין לא נכתב בגלל כשל הולידציה של ה-Token (atomicity)
        assert "TWILIO_ACCOUNT_SID" not in env
        assert "TWILIO_AUTH_TOKEN" not in env

        # וגם os.environ — ה-handler משנה אותו ישירות, אז נוודא שלא דלף
        import os
        assert os.environ.get("TWILIO_ACCOUNT_SID", "") == ""
