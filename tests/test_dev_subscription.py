"""
טסטים למסך /dev/subscription ול-auth של איזור המפתח.

שני סטים של בדיקות:
- כשה-DEVELOPER_PASSWORD לא מוגדר → כל הראוטים מחזירים 404 (לא חושפים קיום).
- כשה-DEVELOPER_PASSWORD מוגדר → flow מלא של login + ניהול חבילה ופיצ'רים.
"""

import importlib
import json

import pytest


def _build_app(monkeypatch, tmp_path, *, dev_password: str = ""):
    """
    בונה Flask test app טרי לכל test, עם DB זמני ו-env מבוקר.
    מחזיר test_client.
    """
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "adminpass123")
    monkeypatch.setenv("ADMIN_SECRET_KEY", "test-secret")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DEVELOPER_PASSWORD", dev_password)
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
    import admin.app as _admin_app
    importlib.reload(_admin_app)

    from admin.app import create_admin_app
    app = create_admin_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    return app.test_client()


# ── סט 1: DEVELOPER_PASSWORD לא מוגדר ────────────────────────────────────


class TestDevAccessDisabled:
    """כש-env DEVELOPER_PASSWORD ריק — איזור /dev/* לא נגיש (404)."""

    def test_dev_login_get_returns_404(self, tmp_path, monkeypatch):
        client = _build_app(monkeypatch, tmp_path, dev_password="")
        resp = client.get("/dev/login")
        assert resp.status_code == 404

    def test_dev_login_post_returns_404(self, tmp_path, monkeypatch):
        client = _build_app(monkeypatch, tmp_path, dev_password="")
        resp = client.post("/dev/login", data={"password": "anything"})
        assert resp.status_code == 404

    def test_dev_subscription_returns_404(self, tmp_path, monkeypatch):
        client = _build_app(monkeypatch, tmp_path, dev_password="")
        resp = client.get("/dev/subscription")
        assert resp.status_code == 404

    def test_dev_set_plan_returns_404(self, tmp_path, monkeypatch):
        client = _build_app(monkeypatch, tmp_path, dev_password="")
        resp = client.post(
            "/dev/subscription/set-plan", data={"plan": "premium", "reason": "x"}
        )
        assert resp.status_code == 404


# ── סט 2: DEVELOPER_PASSWORD מוגדר ────────────────────────────────────────


@pytest.fixture
def dev_client(tmp_path, monkeypatch):
    """test client עם DEVELOPER_PASSWORD מוגדר."""
    return _build_app(monkeypatch, tmp_path, dev_password="devpass-secret-123")


class TestDevLogin:
    def test_login_page_loads(self, dev_client):
        resp = dev_client.get("/dev/login")
        assert resp.status_code == 200
        assert b"\xd7\x90\xd7\x99\xd7\x96\xd7\x95\xd7\xa8" in resp.data  # "איזור" UTF-8

    def test_wrong_password_fails(self, dev_client):
        resp = dev_client.post(
            "/dev/login", data={"password": "wrong"}, follow_redirects=False
        )
        # נשאר ב-/dev/login עם flash error
        assert resp.status_code == 200

    def test_correct_password_redirects_to_subscription(self, dev_client):
        resp = dev_client.post(
            "/dev/login",
            data={"password": "devpass-secret-123"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "/dev/subscription" in resp.headers.get("Location", "")

    def test_unauthenticated_subscription_redirects_to_login(self, dev_client):
        resp = dev_client.get("/dev/subscription", follow_redirects=False)
        assert resp.status_code == 302
        assert "/dev/login" in resp.headers.get("Location", "")


class TestDevSubscriptionPage:
    def _login(self, client):
        client.post("/dev/login", data={"password": "devpass-secret-123"})

    def test_page_renders_after_login(self, dev_client):
        self._login(dev_client)
        resp = dev_client.get("/dev/subscription")
        assert resp.status_code == 200
        # מציג את שמות החבילות
        assert b"basic" in resp.data
        assert b"premium" in resp.data

    def test_set_plan_to_premium(self, dev_client):
        self._login(dev_client)
        resp = dev_client.post(
            "/dev/subscription/set-plan",
            data={"plan": "premium", "reason": "test upgrade"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        # ודאו שהשינוי נשמר ב-DB
        import feature_flags
        assert feature_flags.get_current_plan() == "premium"
        assert feature_flags.has_feature("broadcast") is True

    def test_invalid_plan_rejected(self, dev_client):
        self._login(dev_client)
        import feature_flags
        # ברירת המחדל היא premium — קובעים basic כבסיס ידוע לבדיקה
        feature_flags.set_plan("basic", reason="test baseline")
        resp = dev_client.post(
            "/dev/subscription/set-plan",
            data={"plan": "enterprise", "reason": ""},
            follow_redirects=False,
        )
        # redirect חזרה למסך עם flash שגיאה
        assert resp.status_code == 302
        # החבילה נשארה basic (הבקשה הלא-חוקית נדחתה)
        assert feature_flags.get_current_plan() == "basic"

    def test_override_feature_true(self, dev_client):
        self._login(dev_client)
        import feature_flags
        # ברירת המחדל היא premium — קובעים basic כדי לבדוק override על גבי basic
        feature_flags.set_plan("basic", reason="test baseline")
        resp = dev_client.post(
            "/dev/subscription/override",
            data={"feature": "broadcast", "value": "true"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        # basic + override broadcast=true → has_feature == True
        assert feature_flags.get_current_plan() == "basic"
        assert feature_flags.has_feature("broadcast") is True

    def test_override_feature_numeric(self, dev_client):
        self._login(dev_client)
        resp = dev_client.post(
            "/dev/subscription/override",
            data={"feature": "scenarios_max", "value": "10"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        import feature_flags
        assert feature_flags.get_feature_value("scenarios_max") == 10

    def test_override_feature_null(self, dev_client):
        self._login(dev_client)
        # קודם override ל-5
        dev_client.post(
            "/dev/subscription/override",
            data={"feature": "scenarios_max", "value": "5"},
        )
        # עכשיו null
        dev_client.post(
            "/dev/subscription/override",
            data={"feature": "scenarios_max", "value": "null"},
        )
        import feature_flags
        assert feature_flags.get_feature_value("scenarios_max") is None

    def test_override_invalid_value_rejected(self, dev_client):
        self._login(dev_client)
        import feature_flags
        # ברירת המחדל premium — קובעים basic (broadcast כבוי) כבסיס לבדיקה
        feature_flags.set_plan("basic", reason="test baseline")
        resp = dev_client.post(
            "/dev/subscription/override",
            data={"feature": "broadcast", "value": "maybe"},
            follow_redirects=False,
        )
        # redirect ולא 500
        assert resp.status_code == 302
        # ערך לא השתנה — basic + ללא override → broadcast לא פעיל
        assert feature_flags.has_feature("broadcast") is False

    def test_override_invalid_feature_name_rejected(self, dev_client):
        self._login(dev_client)
        resp = dev_client.post(
            "/dev/subscription/override",
            data={"feature": "magic_unicorn", "value": "true"},
            follow_redirects=False,
        )
        assert resp.status_code == 302

    def test_reset_feature_removes_override(self, dev_client):
        self._login(dev_client)
        import feature_flags
        # ברירת המחדל premium — קובעים basic כדי שאיפוס יחזיר ל-broadcast כבוי
        feature_flags.set_plan("basic", reason="test baseline")
        # הפעלת override
        dev_client.post(
            "/dev/subscription/override",
            data={"feature": "broadcast", "value": "true"},
        )
        assert feature_flags.has_feature("broadcast") is True
        # איפוס
        resp = dev_client.post(
            "/dev/subscription/reset/broadcast",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        # חזר לברירת המחדל של basic = False
        assert feature_flags.has_feature("broadcast") is False

    def test_logout_clears_session(self, dev_client):
        self._login(dev_client)
        # מאומת
        resp = dev_client.get("/dev/subscription", follow_redirects=False)
        assert resp.status_code == 200
        # logout
        resp = dev_client.post("/dev/logout", follow_redirects=False)
        assert resp.status_code == 302
        # אחרי logout — חזרה ל-login
        resp = dev_client.get("/dev/subscription", follow_redirects=False)
        assert resp.status_code == 302
        assert "/dev/login" in resp.headers.get("Location", "")


class TestPlanHistoryDisplay:
    def test_history_recorded_after_set_plan(self, dev_client):
        dev_client.post("/dev/login", data={"password": "devpass-secret-123"})
        dev_client.post(
            "/dev/subscription/set-plan",
            data={"plan": "advanced", "reason": "trial upgrade"},
        )
        import feature_flags
        history = feature_flags.get_plan_history(limit=10)
        assert len(history) >= 1
        assert history[0]["new_plan"] == "advanced"
        assert "trial upgrade" in history[0]["reason"]

    def test_history_recorded_after_override(self, dev_client):
        dev_client.post("/dev/login", data={"password": "devpass-secret-123"})
        dev_client.post(
            "/dev/subscription/override",
            data={"feature": "broadcast", "value": "true"},
        )
        import feature_flags
        history = feature_flags.get_plan_history(limit=10)
        # נרשמה שורה עם reason שמתחיל ב-override_only
        assert any("override_only" in h["reason"] for h in history)
