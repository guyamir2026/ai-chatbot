"""
טסטים ל-control plane (control_plane.py) — multi-tenant שלב 2.

מכסים: מחזור חיים של tenant (יצירה/סטטוסים/רשימות), סודות מוצפנים
fail-closed, ראוטים, ואכיפת סטטוס דרך tenancy (מושעה ⇒ חסימת DB).
"""

import pytest
from unittest.mock import patch

import control_plane as cp
import tenancy
from tenancy import (
    InvalidTenantSlug,
    TenantSuspendedError,
    UnregisteredTenantError,
    tenant_context,
)


@pytest.fixture
def platform_env(tmp_path):
    """סביבת פלטפורמה מבודדת: DATA_DIR זמני + ניקוי caches בין טסטים."""
    with patch("ai_chatbot.config.DATA_DIR", tmp_path), \
         patch("ai_chatbot.config.DB_PATH", tmp_path / "default.db"):
        cp.invalidate_status_cache()
        yield tmp_path
        cp.invalidate_status_cache()


class TestTenantLifecycle:
    def test_create_tenant_full(self, platform_env):
        cp.create_tenant("salon-a", "מספרת דנה")

        row = cp.get_tenant("salon-a")
        assert row["display_name"] == "מספרת דנה"
        assert row["status"] == "active"
        assert row["plan"] == "premium"

        # ה-data plane נוצר בפועל: קובץ DB עם הסכימה המלאה + seed שעות
        db_file = platform_env / "tenants" / "salon-a" / "chatbot.db"
        assert db_file.exists()
        with tenant_context("salon-a"):
            from ai_chatbot import database as db

            with db.get_connection() as conn:
                hours = conn.execute(
                    "SELECT COUNT(*) AS c FROM business_hours"
                ).fetchone()
                assert hours["c"] > 0  # seed רץ

    def test_create_tenant_duplicate_rejected(self, platform_env):
        cp.create_tenant("salon-a", "א")
        with pytest.raises(cp.TenantExistsError):
            cp.create_tenant("salon-a", "ב")

    def test_default_slug_reserved(self, platform_env):
        with pytest.raises(InvalidTenantSlug):
            cp.create_tenant("default", "legacy")

    def test_invalid_slug_rejected(self, platform_env):
        with pytest.raises(InvalidTenantSlug):
            cp.create_tenant("Bad Slug!", "x")

    def test_status_transitions_and_listing(self, platform_env):
        cp.create_tenant("salon-a", "א")
        cp.create_tenant("salon-b", "ב")
        cp.set_tenant_status("salon-a", "suspended")

        assert {t["tenant_id"] for t in cp.list_tenants()} == {"salon-a", "salon-b"}
        assert [t["tenant_id"] for t in cp.list_tenants(status="active")] == ["salon-b"]

        with pytest.raises(ValueError):
            cp.set_tenant_status("salon-b", "no-such-status")
        with pytest.raises(cp.UnknownTenantError):
            cp.set_tenant_status("ghost", "active")

    def test_schedulable_fallback_and_registry(self, platform_env):
        # אין platform.db בכלל → default בלבד (התנהגות שלב 1)
        assert cp.list_schedulable_tenant_ids() == ["default"]

        cp.create_tenant("salon-a", "א")
        cp.create_tenant("salon-b", "ב")
        cp.set_tenant_status("salon-b", "suspended")
        # יש רישום → רק active רשומים (default ה-legacy לא נכלל)
        assert cp.list_schedulable_tenant_ids() == ["salon-a"]


class TestStatusEnforcement:
    def test_suspended_tenant_blocked_from_db(self, platform_env):
        cp.create_tenant("salon-a", "א")
        cp.set_tenant_status("salon-a", "suspended")

        from ai_chatbot import database as db

        with pytest.raises(TenantSuspendedError):
            with tenant_context("salon-a"):
                with db.get_connection():
                    pass

    def test_migrating_tenant_blocked(self, platform_env):
        cp.create_tenant("salon-a", "א")
        cp.set_tenant_status("salon-a", "migrating")
        with pytest.raises(TenantSuspendedError):
            tenancy.tenant_db_path("salon-a")

    def test_reactivation_unblocks(self, platform_env):
        cp.create_tenant("salon-a", "א")
        cp.set_tenant_status("salon-a", "suspended")
        cp.set_tenant_status("salon-a", "active")
        # לא זורק — ה-cache עבר אינבלידציה בעדכון הסטטוס
        assert tenancy.tenant_db_path("salon-a").name == "chatbot.db"

    def test_unregistered_allowed_when_not_strict(self, platform_env):
        # אין רישום בכלל — מותר (התנהגות פיתוח/טסטים)
        assert tenancy.tenant_db_path("free-tenant").name == "chatbot.db"

    def test_unregistered_blocked_in_strict(self, platform_env, monkeypatch):
        cp.create_tenant("salon-a", "א")  # קיים רישום
        monkeypatch.setenv("TENANCY_STRICT", "true")
        with pytest.raises(UnregisteredTenantError):
            tenancy.tenant_db_path("ghost-tenant")
        # אבל tenant רשום ופעיל עובר גם ב-strict
        assert tenancy.tenant_db_path("salon-a").name == "chatbot.db"

    def test_default_tenant_never_checked(self, platform_env):
        # default ממופה לקבצים ה-legacy ואינו נבדק מול הרישום
        assert tenancy.tenant_db_path("default") == platform_env / "default.db"


class TestSecrets:
    def test_secret_roundtrip_encrypted_at_rest(self, platform_env):
        cp.create_tenant("salon-a", "א")
        cp.set_tenant_secret("salon-a", "telegram_bot_token", "tok-123")

        assert cp.get_tenant_secret("salon-a", "telegram_bot_token") == "tok-123"
        # על הדיסק — מוצפן, לא ניתן לקריאה
        with cp.get_platform_connection() as conn:
            raw = conn.execute(
                "SELECT value_enc FROM tenant_secrets"
            ).fetchone()["value_enc"]
        assert raw.startswith("v1:")
        assert "tok-123" not in raw

    def test_fail_closed_without_key(self, platform_env, monkeypatch):
        from utils.crypto import EncryptionConfigError

        cp.create_tenant("salon-a", "א")
        monkeypatch.delenv("SECRETS_ENCRYPTION_KEY", raising=False)
        with pytest.raises(EncryptionConfigError):
            cp.set_tenant_secret("salon-a", "twilio_auth_token", "x")

    def test_empty_value_deletes(self, platform_env):
        cp.create_tenant("salon-a", "א")
        cp.set_tenant_secret("salon-a", "twilio_auth_token", "x")
        cp.set_tenant_secret("salon-a", "twilio_auth_token", "")
        assert cp.get_tenant_secret("salon-a", "twilio_auth_token") is None
        assert cp.list_tenant_secret_names("salon-a") == []

    def test_secret_for_unknown_tenant_rejected(self, platform_env):
        cp.init_platform_db()
        with pytest.raises(cp.UnknownTenantError):
            cp.set_tenant_secret("ghost", "telegram_bot_token", "x")

    def test_invalid_secret_name_rejected(self, platform_env):
        cp.create_tenant("salon-a", "א")
        with pytest.raises(ValueError):
            cp.set_tenant_secret("salon-a", "Bad Name!", "x")

    def test_missing_secret_returns_none(self, platform_env):
        cp.create_tenant("salon-a", "א")
        assert cp.get_tenant_secret("salon-a", "telegram_bot_token") is None


class TestRoutes:
    def test_route_roundtrip(self, platform_env):
        cp.create_tenant("salon-a", "א")
        cp.set_route("twilio_number", "+14155551234", "salon-a")
        assert cp.resolve_route("twilio_number", "+14155551234") == "salon-a"
        assert cp.resolve_route("twilio_number", "+00000000000") is None

    def test_route_repoint(self, platform_env):
        """מפתח הוא natural key — הצבעה מחדש דורסת (INSERT OR REPLACE)."""
        cp.create_tenant("salon-a", "א")
        cp.create_tenant("salon-b", "ב")
        cp.set_route("twilio_number", "+14155551234", "salon-a")
        cp.set_route("twilio_number", "+14155551234", "salon-b")
        assert cp.resolve_route("twilio_number", "+14155551234") == "salon-b"

    def test_route_unknown_type_rejected(self, platform_env):
        cp.create_tenant("salon-a", "א")
        with pytest.raises(ValueError):
            cp.set_route("no_such_type", "k", "salon-a")

    def test_route_unknown_tenant_rejected(self, platform_env):
        cp.init_platform_db()
        with pytest.raises(cp.UnknownTenantError):
            cp.set_route("widget_key", "k", "ghost")

    def test_delete_route(self, platform_env):
        cp.create_tenant("salon-a", "א")
        cp.set_route("widget_key", "wk-1", "salon-a")
        assert cp.delete_route("widget_key", "wk-1") is True
        assert cp.delete_route("widget_key", "wk-1") is False
        assert cp.resolve_route("widget_key", "wk-1") is None

    def test_generate_route_key_unguessable(self, platform_env):
        keys = {cp.generate_route_key() for _ in range(50)}
        assert len(keys) == 50
        assert all(len(k) >= 24 for k in keys)


class TestCli:
    def test_cli_create_and_list(self, platform_env, capsys):
        import platform_cli

        assert platform_cli.main(["create-tenant", "salon-a", "מספרה"]) == 0
        assert platform_cli.main(["list-tenants"]) == 0
        out = capsys.readouterr().out
        assert "salon-a" in out and "active" in out

    def test_cli_secret_via_stdin(self, platform_env, capsys, monkeypatch):
        import io
        import platform_cli

        platform_cli.main(["create-tenant", "salon-a", "מספרה"])
        monkeypatch.setattr("sys.stdin", io.StringIO("sekrit\n"))
        assert platform_cli.main(["set-secret", "salon-a", "telegram_bot_token"]) == 0
        assert cp.get_tenant_secret("salon-a", "telegram_bot_token") == "sekrit"
        # list-secrets מדפיס שמות בלבד
        capsys.readouterr()
        platform_cli.main(["list-secrets", "salon-a"])
        out = capsys.readouterr().out
        assert "telegram_bot_token" in out
        assert "sekrit" not in out

    def test_cli_error_returns_nonzero(self, platform_env):
        import platform_cli

        platform_cli.main(["create-tenant", "salon-a", "א"])
        assert platform_cli.main(["create-tenant", "salon-a", "שוב"]) == 1
