"""
טסטים לאדמין הרב-משתמשים (multi-tenant שלב 2).

מכסים: ‏CRUD של admin_users ב-control plane (כולל דפוסי האבטחה:
hash לעולם לא מוחזר, בדיקת סיסמה גם למשתמש לא-קיים), זרימת ה-login
(legacy env + משתמשי פלטפורמה), קשירת ה-tenant ל-session, מסך
הפלטפורמה ומעבר "פעל-כ", וניתוק owner של tenant שהושעה.
"""

from unittest.mock import patch

import pytest

import control_plane as cp
from tenancy import tenant_context


@pytest.fixture
def platform_env(tmp_path):
    """סביבת פלטפורמה עם שני tenants ומשתמשי אדמין."""
    with patch("ai_chatbot.config.DATA_DIR", tmp_path), \
         patch("ai_chatbot.config.DB_PATH", tmp_path / "default.db"):
        cp.invalidate_status_cache()
        from ai_chatbot import database as _db

        _db.init_db()
        cp.create_tenant("salon-a", "מספרת דנה")
        cp.create_tenant("salon-b", "מכון ב")
        cp.create_admin_user("owner-a@example.com", "password-a1", "owner", "salon-a")
        cp.create_admin_user("amir@platform.com", "platform-pw1", "platform_admin")
        yield tmp_path
        cp.invalidate_status_cache()


def _make_app():
    """אפליקציית אדמין לטסט — patch על קבועי ה-boot הקפואים + כיבוי CSRF."""
    import admin.app as admin_app

    with patch.object(admin_app, "ADMIN_SECRET_KEY", "test-secret"), \
         patch.object(admin_app, "ADMIN_USERNAME", "admin"), \
         patch.object(admin_app, "ADMIN_PASSWORD", "legacy-pw"):
        app = admin_app.create_admin_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    return app


class TestAdminUserCrud:
    def test_create_and_verify_owner(self, platform_env):
        user = cp.verify_admin_login("owner-a@example.com", "password-a1")
        assert user is not None
        assert user["role"] == "owner"
        assert user["tenant_id"] == "salon-a"
        assert "password_hash" not in user  # דפוס #6 — hash לא יוצא החוצה

    def test_email_normalized(self, platform_env):
        assert cp.verify_admin_login("  Owner-A@Example.COM ", "password-a1")

    def test_wrong_password_and_unknown_user_uniform_none(self, platform_env):
        assert cp.verify_admin_login("owner-a@example.com", "wrong") is None
        assert cp.verify_admin_login("ghost@example.com", "whatever") is None
        assert cp.verify_admin_login("not-an-email", "whatever") is None

    def test_disabled_user_rejected(self, platform_env):
        cp.set_admin_user_status("owner-a@example.com", "disabled")
        assert cp.verify_admin_login("owner-a@example.com", "password-a1") is None
        cp.set_admin_user_status("owner-a@example.com", "active")
        assert cp.verify_admin_login("owner-a@example.com", "password-a1")

    def test_owner_requires_registered_tenant(self, platform_env):
        with pytest.raises(cp.UnknownTenantError):
            cp.create_admin_user("x@example.com", "password-x1", "owner", "ghost")
        with pytest.raises(cp.UnknownTenantError):
            cp.create_admin_user("x@example.com", "password-x1", "owner", None)

    def test_short_password_rejected(self, platform_env):
        with pytest.raises(ValueError):
            cp.create_admin_user("x@example.com", "short", "owner", "salon-a")

    def test_duplicate_email_rejected(self, platform_env):
        with pytest.raises(ValueError):
            cp.create_admin_user(
                "owner-a@example.com", "password-x1", "owner", "salon-b",
            )

    def test_list_excludes_hash(self, platform_env):
        users = cp.list_admin_users()
        assert len(users) == 2
        assert all("password_hash" not in u for u in users)
        only_a = cp.list_admin_users("salon-a")
        assert [u["email"] for u in only_a] == ["owner-a@example.com"]


class TestLoginFlow:
    def test_legacy_env_login_still_works(self, platform_env):
        client = _make_app().test_client()
        with patch("ai_chatbot.config.ADMIN_USERNAME", "admin"), \
             patch("ai_chatbot.config.ADMIN_PASSWORD", "legacy-pw"), \
             patch("ai_chatbot.config.ADMIN_PASSWORD_HASH", ""):
            resp = client.post(
                "/login", data={"username": "admin", "password": "legacy-pw"},
            )
        assert resp.status_code == 302
        with client.session_transaction() as sess:
            assert sess["logged_in"] is True
            assert "tenant_id" not in sess  # ‏legacy = ה-tenant של ברירת המחדל

    def test_owner_login_binds_tenant(self, platform_env):
        client = _make_app().test_client()
        resp = client.post(
            "/login",
            data={"username": "owner-a@example.com", "password": "password-a1"},
        )
        assert resp.status_code == 302
        with client.session_transaction() as sess:
            assert sess["logged_in"] is True
            assert sess["tenant_id"] == "salon-a"
            assert sess["admin_role"] == "owner"

    def test_owner_pages_operate_on_tenant_db(self, platform_env):
        """‏E2E: בעל עסק מוסיף רשומת ידע — היא נכתבת ל-DB של העסק שלו בלבד."""
        from ai_chatbot import database as db

        client = _make_app().test_client()
        client.post(
            "/login",
            data={"username": "owner-a@example.com", "password": "password-a1"},
        )
        resp = client.post("/kb/add", data={
            "category": "FAQ",
            "title": "רשומה של בעל העסק א",
            "content": "תוכן",
        }, follow_redirects=True)
        assert resp.status_code == 200

        with tenant_context("salon-a"):
            entries = db.get_all_kb_entries(active_only=False)
            assert any(e["title"] == "רשומה של בעל העסק א" for e in entries)
        with tenant_context("salon-b"):
            entries = db.get_all_kb_entries(active_only=False)
            assert not any(e["title"] == "רשומה של בעל העסק א" for e in entries)

    def test_platform_admin_redirected_to_platform(self, platform_env):
        client = _make_app().test_client()
        resp = client.post(
            "/login",
            data={"username": "amir@platform.com", "password": "platform-pw1"},
        )
        assert resp.status_code == 302
        assert "/platform" in resp.headers["Location"]

    def test_bad_credentials_generic_message(self, platform_env):
        client = _make_app().test_client()
        resp = client.post(
            "/login",
            data={"username": "owner-a@example.com", "password": "wrong"},
            follow_redirects=True,
        )
        assert "פרטי התחברות שגויים" in resp.get_data(as_text=True)

    def test_suspended_tenant_owner_logged_out(self, platform_env):
        client = _make_app().test_client()
        client.post(
            "/login",
            data={"username": "owner-a@example.com", "password": "password-a1"},
        )
        cp.set_tenant_status("salon-a", "suspended")
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]
        with client.session_transaction() as sess:
            assert "logged_in" not in sess


class TestPlatformScreen:
    def _login_platform(self, client):
        client.post(
            "/login",
            data={"username": "amir@platform.com", "password": "platform-pw1"},
        )

    def test_owner_gets_404_on_platform(self, platform_env):
        client = _make_app().test_client()
        client.post(
            "/login",
            data={"username": "owner-a@example.com", "password": "password-a1"},
        )
        assert client.get("/platform").status_code == 404

    def test_platform_admin_sees_tenants(self, platform_env):
        client = _make_app().test_client()
        self._login_platform(client)
        resp = client.get("/platform")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "salon-a" in body and "salon-b" in body

    def test_act_as_switches_panel_tenant(self, platform_env):
        """‏E2E: אחרי 'פעל-כ' — פעולת אדמין נכתבת ל-DB של ה-tenant הנבחר."""
        from ai_chatbot import database as db

        client = _make_app().test_client()
        self._login_platform(client)
        resp = client.post("/platform/act-as/salon-b")
        assert resp.status_code == 302
        client.post("/kb/add", data={
            "category": "FAQ", "title": "נכתב-כ-salon-b", "content": "x",
        })
        with tenant_context("salon-b"):
            entries = db.get_all_kb_entries(active_only=False)
            assert any(e["title"] == "נכתב-כ-salon-b" for e in entries)
        with tenant_context("salon-a"):
            entries = db.get_all_kb_entries(active_only=False)
            assert not any(e["title"] == "נכתב-כ-salon-b" for e in entries)

        # חזרה לתצוגת פלטפורמה
        client.post("/platform/act-as-clear")
        with client.session_transaction() as sess:
            assert "acting_tenant" not in sess

    def test_act_as_inactive_tenant_rejected(self, platform_env):
        client = _make_app().test_client()
        self._login_platform(client)
        cp.set_tenant_status("salon-b", "suspended")
        client.post("/platform/act-as/salon-b")
        with client.session_transaction() as sess:
            assert sess.get("acting_tenant") != "salon-b"

    def test_suspend_from_screen_clears_acting(self, platform_env):
        client = _make_app().test_client()
        self._login_platform(client)
        client.post("/platform/act-as/salon-a")
        client.post(
            "/platform/tenants/salon-a/status", data={"status": "suspended"},
        )
        with client.session_transaction() as sess:
            assert "acting_tenant" not in sess
        assert cp.get_tenant("salon-a")["status"] == "suspended"

    def test_delete_tenant_removes_everything(self, platform_env):
        client = _make_app().test_client()
        self._login_platform(client)
        resp = client.post(
            "/platform/tenants/salon-a/delete", data={"confirm_slug": "salon-a"},
        )
        assert resp.status_code == 302
        # השורה נמחקה; השכן salon-b נשאר
        assert cp.get_tenant("salon-a") is None
        assert cp.get_tenant("salon-b") is not None
        # ה-owner של salon-a נמחק ב-cascade; ה-platform_admin שרד
        assert cp.list_admin_users("salon-a") == []
        assert cp.verify_admin_login("amir@platform.com", "platform-pw1")
        # קבצי ה-data plane נמחקו מהדיסק
        assert not (platform_env / "tenants" / "salon-a").exists()

    def test_delete_wrong_confirm_slug_aborts(self, platform_env):
        client = _make_app().test_client()
        self._login_platform(client)
        resp = client.post(
            "/platform/tenants/salon-a/delete", data={"confirm_slug": "wrong"},
        )
        assert resp.status_code == 302
        assert cp.get_tenant("salon-a") is not None  # לא נמחק

    def test_delete_clears_acting_tenant(self, platform_env):
        client = _make_app().test_client()
        self._login_platform(client)
        client.post("/platform/act-as/salon-a")
        client.post(
            "/platform/tenants/salon-a/delete", data={"confirm_slug": "salon-a"},
        )
        with client.session_transaction() as sess:
            assert "acting_tenant" not in sess
        assert cp.get_tenant("salon-a") is None

    def test_owner_cannot_delete_tenant(self, platform_env):
        client = _make_app().test_client()
        client.post(
            "/login",
            data={"username": "owner-a@example.com", "password": "password-a1"},
        )
        resp = client.post(
            "/platform/tenants/salon-b/delete", data={"confirm_slug": "salon-b"},
        )
        assert resp.status_code == 404
        assert cp.get_tenant("salon-b") is not None  # לא נמחק


class TestCreateAdminCli:
    def test_cli_create_and_list(self, platform_env, capsys, monkeypatch):
        import io

        import platform_cli

        monkeypatch.setattr("sys.stdin", io.StringIO("cli-password1\n"))
        assert platform_cli.main(
            ["create-admin", "cli-owner@example.com", "salon-b"]
        ) == 0
        assert cp.verify_admin_login("cli-owner@example.com", "cli-password1")

        capsys.readouterr()
        platform_cli.main(["list-admins"])
        out = capsys.readouterr().out
        assert "cli-owner@example.com" in out
        assert "cli-password1" not in out  # סיסמה לעולם לא מודפסת

    def test_cli_platform_admin(self, platform_env, capsys, monkeypatch):
        import io

        import platform_cli

        monkeypatch.setattr("sys.stdin", io.StringIO("padmin-pass1\n"))
        assert platform_cli.main(
            ["create-admin", "p@example.com", "--platform"]
        ) == 0
        user = cp.verify_admin_login("p@example.com", "padmin-pass1")
        assert user["role"] == "platform_admin"
        assert user["tenant_id"] is None
