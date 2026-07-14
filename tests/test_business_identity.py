"""
טסטים לזהות עסקית פר-tenant.

מודל הזהות:
- **שם העסק** — מקור אמת יחיד: display_name ב-control plane (נקבע בהקמת
  ה-tenant). ל-tenant של ברירת המחדל (legacy, אין רישום) ⇒ env.
- **כרטיס ביקור** (טלפון/כתובת/אתר) — פר-tenant ב-bot_settings, נערך
  במסך "כרטיס ביקור"; ריק ⇒ fallback ל-env.
"""

from unittest.mock import patch

import config as cfg
from ai_chatbot import database as db


class TestContactFields:
    """טלפון/כתובת/אתר — מ-bot_settings עם fallback ל-env."""

    def test_env_fallback_when_columns_empty(self, db_conn):
        with patch("ai_chatbot.config.BUSINESS_PHONE", "+972-3-1234567"), \
             patch("ai_chatbot.config.BUSINESS_ADDRESS", "כתובת env"):
            biz = cfg.get_business_config()
            assert biz.phone == "+972-3-1234567"
            assert biz.address == "כתובת env"

    def test_db_values_win_over_env(self, db_conn):
        db.update_business_identity(
            phone="+972-50-7654321", address="הרצל 1, תל אביב",
            website="https://ruth.example",
        )
        with patch("ai_chatbot.config.BUSINESS_PHONE", "+972-3-0000000"):
            biz = cfg.get_business_config()
        assert biz.phone == "+972-50-7654321"
        assert biz.address == "הרצל 1, תל אביב"
        assert biz.website == "https://ruth.example"

    def test_partial_fallback_per_field(self, db_conn):
        db.update_business_identity(phone="+972-50-1111111")
        with patch("ai_chatbot.config.BUSINESS_ADDRESS", "כתובת env"):
            biz = cfg.get_business_config()
        assert biz.phone == "+972-50-1111111"
        assert biz.address == "כתובת env"

    def test_clearing_returns_to_env(self, db_conn):
        db.update_business_identity(phone="+972-50-9999999")
        db.update_business_identity(phone="")
        with patch("ai_chatbot.config.BUSINESS_PHONE", "+972-3-0000000"):
            assert cfg.get_business_config().phone == "+972-3-0000000"

    def test_none_leaves_field_untouched(self, db_conn):
        db.update_business_identity(phone="+972-50-1111111")
        db.update_business_identity(website="https://x.example")  # phone=None
        biz = cfg.get_business_config()
        assert biz.phone == "+972-50-1111111"
        assert biz.website == "https://x.example"

    def test_db_failure_falls_back_to_env(self):
        with patch("ai_chatbot.database.get_bot_settings",
                   side_effect=RuntimeError("db down")), \
             patch("ai_chatbot.config.BUSINESS_PHONE", "+972-3-0000000"):
            # אין tenant context ⇒ default ⇒ שם מ-env, וטלפון מ-env (כשל DB)
            assert cfg.get_business_config().phone == "+972-3-0000000"


class TestBusinessName:
    """שם העסק — מ-display_name (control plane) עם fallback ל-env."""

    def test_default_tenant_name_from_env(self, db_conn):
        """אין רישום בפלטפורמה (default) ⇒ השם מגיע מ-env."""
        with patch("ai_chatbot.config.BUSINESS_NAME", "עסק מ-env"):
            assert cfg.get_business_config().name == "עסק מ-env"

    def test_platform_tenant_name_from_display_name(self, tmp_path):
        import control_plane as cp
        from tenancy import tenant_context

        with patch("ai_chatbot.config.DATA_DIR", tmp_path), \
             patch("ai_chatbot.config.DB_PATH", tmp_path / "default.db"):
            cp.invalidate_status_cache()
            db.init_db()
            cp.create_tenant("salon-name", "מספרת רות")
            # השם מגיע ישירות מ-display_name — בלי לזרוע ל-tenant DB
            with tenant_context("salon-name"), \
                 patch("ai_chatbot.config.BUSINESS_NAME", "עסק מ-env"):
                assert cfg.get_business_config().name == "מספרת רות"
            cp.invalidate_status_cache()

    def test_build_system_prompt_uses_display_name(self, tmp_path):
        import control_plane as cp
        from tenancy import tenant_context

        with patch("ai_chatbot.config.DATA_DIR", tmp_path), \
             patch("ai_chatbot.config.DB_PATH", tmp_path / "default.db"):
            cp.invalidate_status_cache()
            db.init_db()
            cp.create_tenant("salon-prompt", "קליניקת אור")
            with tenant_context("salon-prompt"):
                prompt = cfg.build_system_prompt()
            assert "קליניקת אור" in prompt
            cp.invalidate_status_cache()


class TestPerTenantIdentityIsolation:
    def test_two_tenants_different_identity(self, tmp_path):
        """כל tenant רואה את השם שלו (display_name) ואת כרטיס הביקור שלו."""
        import control_plane as cp
        from tenancy import tenant_context

        with patch("ai_chatbot.config.DATA_DIR", tmp_path), \
             patch("ai_chatbot.config.DB_PATH", tmp_path / "default.db"):
            cp.invalidate_status_cache()
            db.init_db()
            cp.create_tenant("salon-a", "מספרת אביב")
            cp.create_tenant("salon-b", "קליניקת בר")

            with tenant_context("salon-a"):
                assert cfg.get_business_config().name == "מספרת אביב"
                db.update_business_identity(phone="+972-50-1112222")
            with tenant_context("salon-b"):
                assert cfg.get_business_config().name == "קליניקת בר"
                # כרטיס הביקור מבודד — הטלפון של salon-a לא דלף לכאן
                assert cfg.get_business_config().phone != "+972-50-1112222"
            cp.invalidate_status_cache()
