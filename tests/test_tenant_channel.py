"""
טסטים לערוץ פר-tenant — subscription.channel + נעילה אוטומטית.

מכסים: get/set_channel (roundtrip, ולידציה, ברירת מחדל ריקה), בידוד בין
tenants, get_tenant_owner, get_tenant_channel_identity, ומחיקת נתוני ערוץ
במעבר (delete_tenant_channel_data).
"""

import pytest
from unittest.mock import patch

import feature_flags as ff


class TestChannelAccessors:
    def test_default_is_empty(self, db_conn):
        """DB טרי — הערוץ טרם נקבע ⇒ '' (שני המקטעים פתוחים)."""
        assert ff.get_channel() == ""

    def test_set_and_get_roundtrip(self, db_conn):
        ff.set_channel("telegram")
        assert ff.get_channel() == "telegram"
        ff.set_channel("whatsapp")
        assert ff.get_channel() == "whatsapp"

    def test_unlock_with_empty(self, db_conn):
        """'' = שחרור הנעילה (מנהל הפלטפורמה ב-/platform)."""
        ff.set_channel("telegram")
        ff.set_channel("")
        assert ff.get_channel() == ""

    def test_invalid_channel_raises(self, db_conn):
        with pytest.raises(ValueError):
            ff.set_channel("smoke-signals")

    def test_get_never_raises_without_db(self):
        """כשל DB ⇒ '' דרך get_subscription_row (never-raise), לא חריגה."""
        with patch("feature_flags.get_subscription_row",
                   side_effect=None, return_value={}):
            assert ff.get_channel() == ""

    def test_invalid_db_value_treated_as_unset(self, db_conn):
        """ערך לא חוקי שנשתל ידנית ב-DB ⇒ מתנהגים כ'טרם נקבע', בלי קריסה."""
        with patch("feature_flags.get_subscription_row",
                   return_value={"channel": "carrier-pigeon"}):
            assert ff.get_channel() == ""


class TestPerTenantChannelIsolation:
    def test_two_tenants_independent_channels(self, tmp_path):
        import control_plane as cp
        from ai_chatbot import database as db
        from tenancy import tenant_context

        with patch("ai_chatbot.config.DATA_DIR", tmp_path), \
             patch("ai_chatbot.config.DB_PATH", tmp_path / "default.db"):
            cp.invalidate_status_cache()
            db.init_db()
            cp.create_tenant("t-one", "אחד")
            cp.create_tenant("t-two", "שתיים")

            with tenant_context("t-one"):
                ff.set_channel("telegram")
            with tenant_context("t-two"):
                assert ff.get_channel() == ""  # לא הושפע מהשכן
                ff.set_channel("whatsapp")
            with tenant_context("t-one"):
                assert ff.get_channel() == "telegram"
            cp.invalidate_status_cache()


class TestTenantOwner:
    def test_owner_lookup_and_no_hash(self, tmp_path):
        """get_tenant_owner מחזיר את ה-owner — בלי password_hash (דפוס #6)."""
        import control_plane as cp
        from ai_chatbot import database as db

        with patch("ai_chatbot.config.DATA_DIR", tmp_path), \
             patch("ai_chatbot.config.DB_PATH", tmp_path / "default.db"):
            cp.invalidate_status_cache()
            db.init_db()
            cp.create_tenant("t-own", "בעלים")
            assert cp.get_tenant_owner("t-own") is None  # עוד אין owner
            cp.create_admin_user(
                "owner@biz.example", "s3cret-pass", role="owner",
                tenant_id="t-own", display_name="בעלים",
            )
            owner = cp.get_tenant_owner("t-own")
            assert owner is not None
            assert owner["email"] == "owner@biz.example"
            assert "password_hash" not in owner
            cp.invalidate_status_cache()


class TestChannelIdentityAndDeletion:
    def test_identity_default_tenant_from_env(self, tmp_path):
        import control_plane as cp

        with patch("ai_chatbot.config.DATA_DIR", tmp_path), \
             patch("ai_chatbot.config.TELEGRAM_BOT_USERNAME", "EnvBot"), \
             patch("ai_chatbot.config.TWILIO_WHATSAPP_NUMBER", "whatsapp:+972999"):
            identity = cp.get_tenant_channel_identity("default")
            assert identity["telegram_bot_username"] == "EnvBot"
            assert identity["whatsapp_number"] == "whatsapp:+972999"

    def test_identity_platform_tenant_from_secrets_and_switch_deletes(
        self, tmp_path, monkeypatch
    ):
        """זהות מהסודות + מעבר ערוץ מוחק את נתוני הערוץ הקודם."""
        import control_plane as cp
        from ai_chatbot import database as db

        # מפתח הצפנה לסודות (fail-closed בלעדיו)
        from cryptography.fernet import Fernet
        monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", Fernet.generate_key().decode())

        with patch("ai_chatbot.config.DATA_DIR", tmp_path), \
             patch("ai_chatbot.config.DB_PATH", tmp_path / "default.db"):
            cp.invalidate_status_cache()
            db.init_db()
            cp.create_tenant("t-sw", "מעבר")
            cp.set_tenant_secret("t-sw", "telegram_bot_token", "123:abc")
            cp.set_tenant_secret("t-sw", "telegram_bot_username", "SwBot")
            cp.set_route("telegram_webhook_key", "tg-key-1", "t-sw")

            identity = cp.get_tenant_channel_identity("t-sw")
            assert identity["telegram_bot_username"] == "SwBot"
            assert identity["whatsapp_number"] == ""

            # מעבר ל-WhatsApp ⇒ מוחקים את צד הטלגרם: סודות + ראוט
            cp.delete_tenant_channel_data("t-sw", "telegram")
            assert cp.get_tenant_secret("t-sw", "telegram_bot_token") is None
            assert cp.get_tenant_secret("t-sw", "telegram_bot_username") is None
            assert cp.get_tenant_route_key("t-sw", "telegram_webhook_key") is None
            # אין נתוני whatsapp — מחיקה נוספת היא no-op ולא חריגה
            cp.delete_tenant_channel_data("t-sw", "whatsapp")
            cp.invalidate_status_cache()

    def test_delete_unknown_channel_raises(self, tmp_path):
        import control_plane as cp
        from ai_chatbot import database as db

        with patch("ai_chatbot.config.DATA_DIR", tmp_path), \
             patch("ai_chatbot.config.DB_PATH", tmp_path / "default.db"):
            cp.invalidate_status_cache()
            db.init_db()
            cp.create_tenant("t-x", "איקס")
            with pytest.raises(ValueError):
                cp.delete_tenant_channel_data("t-x", "meta")
            cp.invalidate_status_cache()
