"""
טסטים לחסימת UI ב-Phase 4 — sidebar links עם מנעול ומודאל שדרג.

הטסטים בודקים:
- ב-basic: לינקי broadcast/followup מסומנים `locked-feature` ולא לינקים אמיתיים.
- ב-premium: אותם לינקים אמיתיים.
- ה-modal של "שדרג" נכלל בכל עמוד.
- ה-mapping של feature → required plan נטען לדף.
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
    # ברירת המחדל היא premium; קובעים מפורשות (כולל basic) כדי לבדוק את חסימת ה-UI.
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


class TestSidebarLockedInBasic:
    """ב-basic, broadcast ו-followup_24h לא מופעלים — אמורים להופיע נעולים."""

    def test_dashboard_shows_locked_broadcast(self, tmp_path, monkeypatch):
        client = _build_app(monkeypatch, tmp_path, plan="basic")
        _login(client)
        resp = client.get("/")
        body = resp.data.decode("utf-8")
        # הלינק קיים אבל מסומן locked-feature ולא href אמיתי
        assert 'locked-feature' in body
        assert 'data-feature="broadcast"' in body
        # bi-lock-fill מציין שיש מנעול ויזואלי
        assert 'bi-lock-fill' in body

    def test_dashboard_shows_locked_followup(self, tmp_path, monkeypatch):
        client = _build_app(monkeypatch, tmp_path, plan="basic")
        _login(client)
        resp = client.get("/")
        body = resp.data.decode("utf-8")
        assert 'data-feature="followup_24h"' in body

    def test_basic_does_not_render_real_broadcast_link(self, tmp_path, monkeypatch):
        client = _build_app(monkeypatch, tmp_path, plan="basic")
        _login(client)
        resp = client.get("/")
        body = resp.data.decode("utf-8")
        # אין href="/broadcast" אמיתי — רק javascript:void(0)
        # נחפש את הקישור הספציפי לסיידבר (בתוך ul.sidebar-nav)
        # פשוט יותר: אין מחרוזת `href="/broadcast"` עם quote-mark נכון
        # כי הסיידבר עוטף אותם ב-locked-feature.
        assert 'href="/broadcast"' not in body
        assert 'href="/broadcast/templates"' not in body
        assert 'href="/broadcast/campaigns"' not in body


class TestSidebarUnlockedInPremium:
    """ב-premium, broadcast/followup פעילים — לינקים אמיתיים."""

    def test_premium_shows_real_broadcast_links(self, tmp_path, monkeypatch):
        client = _build_app(monkeypatch, tmp_path, plan="premium")
        _login(client)
        resp = client.get("/")
        body = resp.data.decode("utf-8")
        assert 'href="/broadcast"' in body
        assert 'href="/broadcast/templates"' in body
        assert 'href="/broadcast/campaigns"' in body
        # אין מנעול על הקישורים האלה (אבל ייתכן שיש על אחרים — לא בודקים כאן)

    def test_premium_does_not_lock_followup(self, tmp_path, monkeypatch):
        client = _build_app(monkeypatch, tmp_path, plan="premium")
        _login(client)
        resp = client.get("/")
        body = resp.data.decode("utf-8")
        assert 'href="/followups"' in body

    def test_advanced_unlocks_followup_but_not_broadcast(self, tmp_path, monkeypatch):
        client = _build_app(monkeypatch, tmp_path, plan="advanced")
        _login(client)
        resp = client.get("/")
        body = resp.data.decode("utf-8")
        # followup פתוח ב-advanced
        assert 'href="/followups"' in body
        # broadcast עדיין נעול
        assert 'data-feature="broadcast"' in body
        assert 'href="/broadcast"' not in body


class TestUpgradeModalIncluded:
    """המודאל של 'שדרג' חייב להופיע בכל עמוד שמרחיב את base.html."""

    def test_modal_present_on_dashboard(self, tmp_path, monkeypatch):
        client = _build_app(monkeypatch, tmp_path, plan="basic")
        _login(client)
        resp = client.get("/")
        body = resp.data.decode("utf-8")
        assert 'id="upgrade-modal"' in body
        assert 'showUpgradeModal' in body
        assert 'closeUpgradeModal' in body

    def test_required_plans_mapping_in_js(self, tmp_path, monkeypatch):
        """ה-JS חייב להכיל את המיפוי feature → display_name."""
        client = _build_app(monkeypatch, tmp_path, plan="basic")
        _login(client)
        resp = client.get("/")
        body = resp.data.decode("utf-8")
        assert '__upgradeModalRequiredPlans' in body
        # broadcast → "מקצועי" (display_name של premium)
        assert '"broadcast": "מקצועי"' in body
        # followup_24h → "מתקדם"
        assert '"followup_24h": "מתקדם"' in body

    def test_universal_features_not_in_mapping(self, tmp_path, monkeypatch):
        """
        פיצ'רים פעילים בכל החבילות (calendar_sync, scenarios_max) לא
        אמורים להופיע ב-mapping — אין להם 'minimum plan'. הצגתם הייתה
        מטעה (Cursor bot fix).
        """
        client = _build_app(monkeypatch, tmp_path, plan="basic")
        _login(client)
        body = client.get("/").data.decode("utf-8")
        # ה-mapping חייב להיות בלי calendar_sync ו-scenarios_max
        # (הם פעילים בכל החבילות → אין מינימום)
        assert '"calendar_sync"' not in body
        assert '"scenarios_max"' not in body
