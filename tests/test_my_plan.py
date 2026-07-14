"""
טסטים ל-Phase 5 — /my-plan וב-grace banner גלובלי.
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
    # ברירת המחדל היא premium; קובעים מפורשות (כולל basic) כדי שהטסט יבדוק
    # את החבילה שביקש ולא את ברירת המחדל.
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


def _set_plan_started_at(value: str):
    """כתיבה ידנית ל-DB ל-plan_started_at — לבדיקות תקופת חסד."""
    import database
    with database.get_connection() as conn:
        conn.execute(
            "UPDATE subscription SET plan_started_at = ? WHERE id = 1",
            (value,),
        )


# ── /my-plan: זמינות וגישה ────────────────────────────────────────────────


class TestMyPlanAccess:
    def test_login_required(self, tmp_path, monkeypatch):
        client = _build_app(monkeypatch, tmp_path, plan="basic")
        # ללא login → redirect ל-/login
        resp = client.get("/my-plan", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers.get("Location", "")

    def test_loads_after_login(self, tmp_path, monkeypatch):
        client = _build_app(monkeypatch, tmp_path, plan="basic")
        _login(client)
        resp = client.get("/my-plan")
        assert resp.status_code == 200

    def test_not_feature_gated(self, tmp_path, monkeypatch):
        """אפילו ב-basic — דף החבילה זמין (אין feature gate על העמוד עצמו)."""
        client = _build_app(monkeypatch, tmp_path, plan="basic")
        _login(client)
        resp = client.get("/my-plan", follow_redirects=False)
        assert resp.status_code == 200


# ── /my-plan: תוכן ──────────────────────────────────────────────────────


class TestMyPlanContent:
    def test_displays_current_plan_name(self, tmp_path, monkeypatch):
        client = _build_app(monkeypatch, tmp_path, plan="advanced")
        _login(client)
        body = client.get("/my-plan").data.decode("utf-8")
        # הצגת שם החבילה (display_name של advanced = "מתקדם")
        assert "מתקדם" in body

    def test_basic_shows_locked_premium_features(self, tmp_path, monkeypatch):
        client = _build_app(monkeypatch, tmp_path, plan="basic")
        _login(client)
        body = client.get("/my-plan").data.decode("utf-8")
        # broadcast חייב להופיע נעול (ב-basic) ועם החבילה המינימלית "מקצועי"
        assert "נעול" in body
        assert "מקצועי" in body
        # ה-labels בעברית
        assert "שליחת broadcasts ידנית" in body
        assert "פולואפ אוטומטי 24h" in body

    def test_my_plan_does_not_list_universal_features(self, tmp_path, monkeypatch):
        """
        calendar_sync ו-scenarios_max פעילים בכל החבילות (universal) —
        לא צריכים להופיע בטבלה של בעל העסק (מיותר ומבלבל).
        """
        client = _build_app(monkeypatch, tmp_path, plan="basic")
        _login(client)
        body = client.get("/my-plan").data.decode("utf-8")
        # ה-labels של universal features לא אמורים להופיע
        assert "סנכרון יומן" not in body
        assert "מספר תרחישים" not in body

    def test_premium_shows_features_active(self, tmp_path, monkeypatch):
        client = _build_app(monkeypatch, tmp_path, plan="premium")
        _login(client)
        body = client.get("/my-plan").data.decode("utf-8")
        # לפחות פיצ'ר אחד פעיל
        assert "פעיל" in body

    def test_override_indicator_shown(self, tmp_path, monkeypatch):
        """אם המפתח דרס ידנית פיצ'ר — צריך לראות 'הופעל ידנית'."""
        client = _build_app(monkeypatch, tmp_path, plan="basic")
        # override broadcast=true
        import feature_flags
        feature_flags.override_feature("broadcast", True)
        _login(client)
        body = client.get("/my-plan").data.decode("utf-8")
        assert "הופעל ידנית" in body


# ── Grace banner ──────────────────────────────────────────────────────────


class TestGraceBanner:
    """באנר תקופת החסד הוסר (מוצר חד-שכבתי) — מוודאים שאינו מוצג בשום מצב."""

    def test_no_banner_when_well_in_grace(self, tmp_path, monkeypatch):
        client = _build_app(monkeypatch, tmp_path, plan="basic")
        _login(client)
        body = client.get("/").data.decode("utf-8")
        assert "grace-banner" not in body

    def test_no_banner_even_when_grace_ended(self, tmp_path, monkeypatch):
        """אפילו כשהחסד הסתיים (plan_started_at ישן) — הבאנר לא חוזר."""
        client = _build_app(monkeypatch, tmp_path, plan="basic")
        _set_plan_started_at("2020-01-01 00:00:00")
        _login(client)
        body = client.get("/").data.decode("utf-8")
        assert "grace-banner" not in body

    def test_no_banner_on_my_plan_page(self, tmp_path, monkeypatch):
        """גם בעמוד /my-plan עצמו — אין באנר חסד."""
        client = _build_app(monkeypatch, tmp_path, plan="basic")
        _set_plan_started_at("2020-01-01 00:00:00")
        _login(client)
        body = client.get("/my-plan").data.decode("utf-8")
        assert "grace-banner" not in body


# ── סיידבר: לינק "החבילה שלי" ───────────────────────────────────────────


class TestMyPlanInSidebar:
    def test_sidebar_link_absent(self, tmp_path, monkeypatch):
        """עמוד "החבילה שלי" מוסתר מהתפריט (הסתרה רכה — ה-route עצמו עדיין קיים)."""
        client = _build_app(monkeypatch, tmp_path, plan="basic")
        _login(client)
        body = client.get("/").data.decode("utf-8")
        assert "החבילה שלי" not in body


# ── feature_flags.is_grace_ended() ──────────────────────────────────────


class TestIsGraceEnded:
    def test_false_when_no_plan_started_at(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
        import config as _root_config
        importlib.reload(_root_config)
        import ai_chatbot.config
        importlib.reload(ai_chatbot.config)
        import database
        importlib.reload(database)
        database.init_db()
        import feature_flags
        importlib.reload(feature_flags)
        # ננקה את plan_started_at
        with database.get_connection() as conn:
            conn.execute(
                "UPDATE subscription SET plan_started_at = '' WHERE id = 1"
            )
        assert feature_flags.is_grace_ended() is False

    def test_false_during_grace(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
        import config as _root_config
        importlib.reload(_root_config)
        import ai_chatbot.config
        importlib.reload(ai_chatbot.config)
        import database
        importlib.reload(database)
        database.init_db()
        import feature_flags
        importlib.reload(feature_flags)
        # מיגרציה כותבת plan_started_at = now → אנחנו בחסד
        assert feature_flags.is_grace_ended() is False

    def test_true_when_grace_period_passed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
        import config as _root_config
        importlib.reload(_root_config)
        import ai_chatbot.config
        importlib.reload(ai_chatbot.config)
        import database
        importlib.reload(database)
        database.init_db()
        import feature_flags
        importlib.reload(feature_flags)
        with database.get_connection() as conn:
            conn.execute(
                "UPDATE subscription SET plan_started_at = '2020-01-01 00:00:00' WHERE id = 1"
            )
        assert feature_flags.is_grace_ended() is True
