"""
טסטים לתצוגת פרומפט המערכת בפאנל הגדרות הבוט (`/bot-settings`).

מכסה רגרסיה: ה-preview נבנה תמיד עם channel="telegram" (ברירת המחדל של
build_system_prompt) — כך שלקוח WhatsApp-only ראה כללי עיצוב של טלגרם
(תגי HTML), ותיבת ה-override (שמאותחלת עם ה-preview) הזריעה פרומפט טלגרם
שנשלח מילה-במילה לשני הערוצים ⇒ עיצוב שבור ב-WhatsApp. התיקון: הפאנל
מעביר את הערוץ הפעיל (detect_active_channel) ל-build_system_prompt.

הטסטים משתמשים בלקוח הטסט של Flask + ב-`db_conn` fixture שמייצר DB tmp
נפרד לכל טסט.
"""

import importlib

import pytest


@pytest.fixture(autouse=True)
def _admin_env(monkeypatch):
    """משתני סביבה ש-create_admin_app() דורש (ADMIN_USERNAME/PASSWORD).

    זהה לדפוס ב-test_admin_business_profile — patch ישיר על המודולים
    שורד reload שטסטים אחרים בריפו מבצעים.
    """
    monkeypatch.setenv("ADMIN_USERNAME", "test_admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "test_pass_for_unit_tests_only")
    monkeypatch.setenv("ADMIN_SECRET_KEY", "test-secret-key-for-unit-tests")

    for mod_name in ("ai_chatbot.config", "admin.app"):
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            continue
        if hasattr(mod, "ADMIN_USERNAME"):
            monkeypatch.setattr(mod, "ADMIN_USERNAME", "test_admin", raising=False)
        if hasattr(mod, "ADMIN_PASSWORD"):
            monkeypatch.setattr(mod, "ADMIN_PASSWORD", "test_pass_for_unit_tests_only", raising=False)
        if hasattr(mod, "ADMIN_SECRET_KEY"):
            monkeypatch.setattr(mod, "ADMIN_SECRET_KEY", "test-secret-key-for-unit-tests", raising=False)


@pytest.fixture
def client(db_conn):
    """Flask test client עם CSRF off ו-session מאומת (זהה ל-test_admin_business_profile)."""
    from admin.app import create_admin_app

    app = create_admin_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SECRET_KEY"] = "test-secret"

    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess["logged_in"] = True
            sess["username"] = "test_admin"
        yield c


# מחרוזות "חוק ברזל" ייחודיות לכל בלוק עיצוב ב-config._build_formatting_rules.
# מובחנות זו מזו — WhatsApp מזכיר "עיצוב WhatsApp", טלגרם מזכיר "תגי HTML של טלגרם".
# חשוב: לא לבדוק על "טלגרם" גרידא, כי כללי הערוץ של WhatsApp מזכירים
# "אל תזכיר... בוט טלגרם" — כלומר "טלגרם" מופיע גם בפרומפט WhatsApp.
WHATSAPP_FORMATTING_RULE = "השתמש אך ורק בעיצוב WhatsApp"
TELEGRAM_FORMATTING_RULE = "השתמש אך ורק בתגי HTML של טלגרם"


def _set_channel_env(monkeypatch, *, telegram_token: str, twilio: bool):
    """קביעת ה-credentials שקובעים את הערוץ הפעיל.

    detect_active_channel קורא את הערכים כתכונות של מודול config
    (getattr(_cfg, ...)), לכן patch ישיר על המודול — setenv לבד לא מספיק
    כי הקבועים כבר נקבעו בעת ייבוא ראשון.
    """
    import ai_chatbot.config as cfg

    monkeypatch.setattr(cfg, "TELEGRAM_BOT_TOKEN", telegram_token, raising=False)
    monkeypatch.setattr(cfg, "TWILIO_ACCOUNT_SID", "ACtest123" if twilio else "", raising=False)
    monkeypatch.setattr(cfg, "TWILIO_AUTH_TOKEN", "authtoken_test" if twilio else "", raising=False)
    monkeypatch.setattr(
        cfg, "TWILIO_WHATSAPP_NUMBER",
        "whatsapp:+14155238886" if twilio else "", raising=False,
    )


class TestBotSettingsPromptChannel:
    """ה-preview בפאנל חייב לשקף את הערוץ הפעיל בפועל."""

    def test_whatsapp_env_shows_whatsapp_formatting(self, client, monkeypatch):
        """פריסת WhatsApp (רק Twilio מוגדר) — preview עם עיצוב WhatsApp, ללא כללי טלגרם."""
        _set_channel_env(monkeypatch, telegram_token="", twilio=True)

        resp = client.get("/bot-settings")

        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert WHATSAPP_FORMATTING_RULE in body
        # הרגרסיה עצמה: אסור שכללי עיצוב הטלגרם יופיעו בפריסת WhatsApp
        assert TELEGRAM_FORMATTING_RULE not in body

    def test_telegram_env_shows_telegram_formatting(self, client, monkeypatch):
        """פריסת טלגרם (רק TELEGRAM_BOT_TOKEN מוגדר) — preview עם עיצוב טלגרם."""
        _set_channel_env(monkeypatch, telegram_token="123456:ABCDEF", twilio=False)

        resp = client.get("/bot-settings")

        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert TELEGRAM_FORMATTING_RULE in body
        assert WHATSAPP_FORMATTING_RULE not in body

    def test_dual_channel_falls_back_to_telegram(self, client, monkeypatch):
        """שני הערוצים מוגדרים (dev) — detect_active_channel מחזיר None,
        וה-preview נופל ל-telegram, זהה לברירת המחדל של ה-runtime."""
        _set_channel_env(monkeypatch, telegram_token="123456:ABCDEF", twilio=True)

        resp = client.get("/bot-settings")

        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert TELEGRAM_FORMATTING_RULE in body
        assert WHATSAPP_FORMATTING_RULE not in body
