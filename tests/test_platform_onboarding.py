"""
טסטים לאשף יצירת tenant ולמסך "הגדרות תשתית" הפר-tenant (multi-tenant).

מכסים: יצירת לקוח (tenant + owner) מ-/platform, שמירת סודות ערוצים
מוצפנים ב-control plane (במקום .env) עבור tenant בפלטפורמה, connect
telegram מהפאנל, שינוי סיסמת owner, ושימור נתיב ה-legacy ל-default.
"""

from unittest.mock import AsyncMock, patch

import pytest

import control_plane as cp
from tenancy import DEFAULT_TENANT


@pytest.fixture
def platform_env(tmp_path):
    with patch("ai_chatbot.config.DATA_DIR", tmp_path), \
         patch("ai_chatbot.config.DB_PATH", tmp_path / "default.db"):
        cp.invalidate_status_cache()
        from ai_chatbot import database as db

        db.init_db()
        cp.create_admin_user("amir@platform.com", "platform-pw1", "platform_admin")
        yield tmp_path
        cp.invalidate_status_cache()


def _make_app():
    import admin.app as admin_app

    with patch.object(admin_app, "ADMIN_SECRET_KEY", "test-secret"), \
         patch.object(admin_app, "ADMIN_USERNAME", "admin"), \
         patch.object(admin_app, "ADMIN_PASSWORD", "legacy-pw"):
        app = admin_app.create_admin_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    return app


def _login_platform(client):
    client.post(
        "/login", data={"username": "amir@platform.com", "password": "platform-pw1"},
    )


def _login_owner(client, email, password):
    client.post("/login", data={"username": email, "password": password})


class TestCreateTenantWizard:
    def test_form_renders_for_platform_admin(self, platform_env):
        client = _make_app().test_client()
        _login_platform(client)
        resp = client.get("/platform/tenants/new")
        assert resp.status_code == 200
        assert "לקוח חדש" in resp.get_data(as_text=True)

    def test_owner_gets_404(self, platform_env):
        cp.create_tenant("salon-a", "א")
        cp.create_admin_user("owner@a.com", "owner-pw1", "owner", "salon-a")
        client = _make_app().test_client()
        _login_owner(client, "owner@a.com", "owner-pw1")
        assert client.get("/platform/tenants/new").status_code == 404

    def test_creates_tenant_and_owner_can_login(self, platform_env):
        client = _make_app().test_client()
        _login_platform(client)
        resp = client.post("/platform/tenants/new", data={
            "slug": "dana-salon",
            "display_name": "מספרת דנה",
            "owner_email": "dana@salon.co.il",
            "owner_password": "dana-pass1",
        })
        assert resp.status_code == 302
        # ה-tenant + ה-DB שלו נוצרו
        assert cp.get_tenant("dana-salon") is not None
        from tenancy import tenant_db_path
        assert tenant_db_path("dana-salon").exists()
        # בעל העסק יכול להתחבר ונקשר ל-tenant שלו
        owner = cp.verify_admin_login("dana@salon.co.il", "dana-pass1")
        assert owner is not None and owner["tenant_id"] == "dana-salon"

    def test_invalid_slug_rejected_no_tenant(self, platform_env):
        client = _make_app().test_client()
        _login_platform(client)
        resp = client.post("/platform/tenants/new", data={
            "slug": "BAD SLUG",
            "display_name": "עסק",
            "owner_email": "x@y.com",
            "owner_password": "password1",
        })
        assert resp.status_code == 200  # חוזר לטופס עם שגיאה
        assert "מזהה לא תקין" in resp.get_data(as_text=True)
        assert cp.list_tenants() == []

    def test_duplicate_slug_rejected(self, platform_env):
        cp.create_tenant("salon-a", "קיים")
        client = _make_app().test_client()
        _login_platform(client)
        resp = client.post("/platform/tenants/new", data={
            "slug": "salon-a",
            "display_name": "כפול",
            "owner_email": "x@y.com",
            "owner_password": "password1",
        })
        assert "כבר קיים" in resp.get_data(as_text=True)


class TestInfraSettingsPerTenant:
    def _setup_owner(self, plan="basic"):
        cp.create_tenant("salon-a", "מספרת דנה", plan=plan)
        cp.create_admin_user("owner@a.com", "owner-pw1", "owner", "salon-a")

    def test_telegram_token_stored_encrypted_not_env(self, platform_env):
        self._setup_owner(plan="basic")  # basic → ערוץ telegram (ה-lock מתיר)
        client = _make_app().test_client()
        _login_owner(client, "owner@a.com", "owner-pw1")

        with patch("ai_chatbot.config.ADMIN_URL", "https://app.example.com"), \
             patch("bot_registry.sync_telegram_webhook", new=AsyncMock()) as m_sync, \
             patch("bot_registry.reset_tenant"):
            resp = client.post("/bot-config", data={
                "form_type": "telegram",
                "telegram_bot_token": "123456:ABC-token",
                "telegram_owner_chat_id": "555",
            })
        assert resp.status_code == 302
        # הסוד נשמר מוצפן ב-control plane של ה-tenant
        assert cp.get_tenant_secret("salon-a", "telegram_bot_token") == "123456:ABC-token"
        assert cp.get_tenant_secret("salon-a", "telegram_owner_chat_id") == "555"
        # webhook נרשם (מפתח + secret נוצרו, sync נקרא)
        assert cp.get_tenant_route_key("salon-a", "telegram_webhook_key")
        assert cp.get_tenant_secret("salon-a", "telegram_webhook_secret")
        m_sync.assert_awaited_once()
        # לא נכתב ל-env של התהליך
        import ai_chatbot.config as _cfg
        assert _cfg.TELEGRAM_BOT_TOKEN != "123456:ABC-token"

    def test_twilio_stored_encrypted_with_route(self, platform_env):
        self._setup_owner(plan="premium")  # premium → ערוץ whatsapp
        client = _make_app().test_client()
        _login_owner(client, "owner@a.com", "owner-pw1")

        with patch("messaging.whatsapp_sender.reset_twilio_clients"):
            resp = client.post("/bot-config", data={
                "form_type": "whatsapp",
                "twilio_account_sid": "AC" + "a" * 32,
                "twilio_auth_token": "b" * 32,
                "twilio_whatsapp_number": "+14155551234",
            })
        assert resp.status_code == 302
        assert cp.get_tenant_secret("salon-a", "twilio_account_sid") == "AC" + "a" * 32
        assert cp.get_tenant_secret("salon-a", "twilio_auth_token") == "b" * 32
        assert cp.get_tenant_secret("salon-a", "twilio_whatsapp_number") == "+14155551234"
        # מפתח webhook נוצר כדי שכתובת הקבלה תהיה זמינה
        assert cp.get_tenant_route_key("salon-a", "twilio_webhook_key")

    def test_owner_password_change_updates_admin_users(self, platform_env):
        self._setup_owner(plan="basic")
        client = _make_app().test_client()
        _login_owner(client, "owner@a.com", "owner-pw1")
        resp = client.post("/bot-config", data={
            "form_type": "admin",
            "admin_username": "ignored",
            "admin_password": "new-owner-pw1",
        })
        assert resp.status_code == 302
        # הסיסמה החדשה עובדת, הישנה לא
        assert cp.verify_admin_login("owner@a.com", "new-owner-pw1")
        assert cp.verify_admin_login("owner@a.com", "owner-pw1") is None

    def test_status_reflects_tenant_secrets(self, platform_env):
        self._setup_owner(plan="basic")
        cp.set_tenant_secret("salon-a", "telegram_bot_token", "ZZ-unique-secret-9")
        client = _make_app().test_client()
        _login_owner(client, "owner@a.com", "owner-pw1")
        resp = client.get("/bot-config")
        assert resp.status_code == 200
        # הטוקן עצמו לא נחשף, אבל הסטטוס "מוגדר" משתקף (write-only)
        assert "ZZ-unique-secret-9" not in resp.get_data(as_text=True)


class TestChannelAutoLock:
    """נעילת ערוץ אוטומטית: חיבור ערוץ אחד נועל את השני, שחרור ב-/platform."""

    def _setup_owner(self):
        cp.create_tenant("salon-a", "מספרת דנה")
        cp.create_admin_user("owner@a.com", "owner-pw1", "owner", "salon-a")

    def _connect_telegram(self, client):
        with patch("ai_chatbot.config.ADMIN_URL", "https://app.example.com"), \
             patch("bot_registry.sync_telegram_webhook", new=AsyncMock(return_value="DanaBot")), \
             patch("bot_registry.reset_tenant"):
            return client.post("/bot-config", data={
                "form_type": "telegram",
                "telegram_bot_token": "123456:ABC-token",
                "telegram_owner_chat_id": "555",
            })

    def test_fresh_tenant_channel_unset(self, platform_env):
        """tenant טרי — ערוץ ריק ⇒ שני המקטעים פתוחים (זה תיקון פער c)."""
        self._setup_owner()
        import feature_flags
        from tenancy import tenant_context

        with tenant_context("salon-a"):
            assert feature_flags.get_channel() == ""

    def test_telegram_connect_locks_whatsapp(self, platform_env):
        self._setup_owner()
        client = _make_app().test_client()
        _login_owner(client, "owner@a.com", "owner-pw1")
        self._connect_telegram(client)

        import feature_flags
        from tenancy import tenant_context

        with tenant_context("salon-a"):
            assert feature_flags.get_channel() == "telegram"
        # שם המשתמש של הבוט נלכד מ-getMe לטובת ה-QR
        assert cp.get_tenant_secret("salon-a", "telegram_bot_username") == "DanaBot"
        # ניסיון לערוך WhatsApp — נחסם (הערוץ נעול)
        resp = client.post("/bot-config", data={
            "form_type": "whatsapp",
            "twilio_account_sid": "AC" + "a" * 32,
            "twilio_auth_token": "b" * 32,
            "twilio_whatsapp_number": "+14155551234",
        }, follow_redirects=True)
        assert "נעולות" in resp.get_data(as_text=True)
        assert cp.get_tenant_secret("salon-a", "twilio_account_sid") is None

    def test_unlock_then_switch_deletes_old_channel_data(self, platform_env):
        """שחרור נעילה ע"י מנהל הפלטפורמה + חיבור WhatsApp ⇒ נתוני
        הטלגרם (סודות + ראוט) נמחקים — לא נשארים credentials רדומים."""
        self._setup_owner()
        owner_client = _make_app().test_client()
        _login_owner(owner_client, "owner@a.com", "owner-pw1")
        self._connect_telegram(owner_client)
        assert cp.get_tenant_secret("salon-a", "telegram_bot_token")

        # שחרור הנעילה — רק platform admin
        admin_client = _make_app().test_client()
        _login_platform(admin_client)
        resp = admin_client.post("/platform/tenants/salon-a/channel-unlock")
        assert resp.status_code == 302
        import feature_flags
        from tenancy import tenant_context

        with tenant_context("salon-a"):
            assert feature_flags.get_channel() == ""

        # חיבור WhatsApp מלא ⇒ ערוץ ננעל ל-whatsapp ונתוני טלגרם נמחקים
        with patch("messaging.whatsapp_sender.reset_twilio_clients"), \
             patch("bot_registry.remove_telegram_webhook", new=AsyncMock()), \
             patch("bot_registry.reset_tenant"):
            owner_client.post("/bot-config", data={
                "form_type": "whatsapp",
                "twilio_account_sid": "AC" + "a" * 32,
                "twilio_auth_token": "b" * 32,
                "twilio_whatsapp_number": "+14155551234",
            })
        with tenant_context("salon-a"):
            assert feature_flags.get_channel() == "whatsapp"
        assert cp.get_tenant_secret("salon-a", "telegram_bot_token") is None
        assert cp.get_tenant_secret("salon-a", "telegram_bot_username") is None
        assert cp.get_tenant_route_key("salon-a", "telegram_webhook_key") is None
        # נתוני ה-WhatsApp החדשים נשמרו
        assert cp.get_tenant_secret("salon-a", "twilio_whatsapp_number") == "+14155551234"

    def test_unlock_requires_platform_admin(self, platform_env):
        self._setup_owner()
        client = _make_app().test_client()
        _login_owner(client, "owner@a.com", "owner-pw1")
        assert client.post(
            "/platform/tenants/salon-a/channel-unlock"
        ).status_code == 404


class TestActAsOwnerIdentity:
    """במצב 'פעל-כ' — מסך הגישה מציג ומעדכן את ה-owner של ה-tenant."""

    def _setup(self):
        cp.create_tenant("salon-a", "מספרת דנה")
        cp.create_admin_user("owner@a.com", "owner-pw1", "owner", "salon-a")

    def _act_as(self, client, slug="salon-a"):
        _login_platform(client)
        client.post(f"/platform/act-as/{slug}")

    def test_bot_config_shows_owner_email_not_platform_admin(self, platform_env):
        """פער e: בעבר הוצג האימייל של מנהל הפלטפורמה במקום של הלקוח."""
        self._setup()
        client = _make_app().test_client()
        self._act_as(client)
        body = client.get("/bot-config").get_data(as_text=True)
        assert "owner@a.com" in body
        assert "amir@platform.com" not in body

    def test_password_change_while_acting_targets_owner(self, platform_env):
        """פער e: שינוי סיסמה במצב פעל-כ מאפס את סיסמת ה-owner,
        לא את סיסמת מנהל הפלטפורמה המחובר."""
        self._setup()
        client = _make_app().test_client()
        self._act_as(client)
        resp = client.post("/bot-config", data={
            "form_type": "admin",
            "admin_username": "ignored",
            "admin_password": "fresh-owner-pw1",
        })
        assert resp.status_code == 302
        # סיסמת ה-owner הוחלפה; סיסמת מנהל הפלטפורמה לא נגעה
        assert cp.verify_admin_login("owner@a.com", "fresh-owner-pw1")
        assert cp.verify_admin_login("owner@a.com", "owner-pw1") is None
        assert cp.verify_admin_login("amir@platform.com", "platform-pw1")


class TestLegacyDefaultTenantUnchanged:
    def test_default_tenant_does_not_touch_control_plane(self, platform_env, tmp_path):
        """login legacy (env) — ה-tenant הוא default; שמירת טוקן לא כותבת
        ל-control plane (נשארת בנתיב ה-env הישן)."""
        client = _make_app().test_client()
        with patch("ai_chatbot.config.ADMIN_USERNAME", "admin"), \
             patch("ai_chatbot.config.ADMIN_PASSWORD", "legacy-pw"), \
             patch("ai_chatbot.config.ADMIN_PASSWORD_HASH", ""):
            client.post("/login", data={"username": "admin", "password": "legacy-pw"})
        import feature_flags
        feature_flags.set_plan("basic")  # ללא context ⇒ ה-tenant של ברירת המחדל
        with patch("dotenv.set_key"):  # לא נוגעים בקובץ .env אמיתי בטסט
            resp = client.post("/bot-config", data={
                "form_type": "telegram",
                "telegram_bot_token": "legacy:token",
                "telegram_owner_chat_id": "111",
            })
        assert resp.status_code == 302
        # שום סוד לא נכתב ל-control plane (הנתיב ה-legacy נבחר)
        assert cp.list_tenant_secret_names(DEFAULT_TENANT) == []

    def test_default_tenant_never_auto_locks(self, platform_env):
        """ה-tenant של ברירת המחדל פטור מהנעילה — שמירת טוקן לא קובעת
        ערוץ (env מנוהל ידנית, dual-channel לבדיקות נשאר אפשרי)."""
        client = _make_app().test_client()
        with patch("ai_chatbot.config.ADMIN_USERNAME", "admin"), \
             patch("ai_chatbot.config.ADMIN_PASSWORD", "legacy-pw"), \
             patch("ai_chatbot.config.ADMIN_PASSWORD_HASH", ""):
            client.post("/login", data={"username": "admin", "password": "legacy-pw"})
        import feature_flags
        with patch("dotenv.set_key"):
            client.post("/bot-config", data={
                "form_type": "telegram",
                "telegram_bot_token": "legacy:token",
                "telegram_owner_chat_id": "111",
            })
        assert feature_flags.get_channel() == ""
