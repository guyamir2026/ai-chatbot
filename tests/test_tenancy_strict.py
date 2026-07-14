"""
בדיקות ל-TENANCY_STRICT — אכיפת fail-loud + אימות שנתיבי הפרודקשן
(boot של main, Flask) עובדים תחת strict.

הרקע: ה-ContextVar מוגדר עם default=None, ו-get_current_tenant נופל ל-
DEFAULT_TENANT אלא אם TENANCY_STRICT דלוק (tenancy.py:84-98). בפרודקשן
מדליקים strict כדי שנתיב ששכח לקבוע context ייכשל רועש במקום להגיש בשקט
את ה-DB של ברירת המחדל. הבדיקות מדליקות strict **פר-טסט** (לא גלובלי),
כדי לא לשבור טסטים אחרים שנשענים על הנפילה הרכה של שלב 1.
"""

from unittest.mock import patch

import pytest

from tenancy import DEFAULT_TENANT, MissingTenantContext, tenant_context


@pytest.fixture
def strict(monkeypatch):
    """מדליק TENANCY_STRICT לטסט הנוכחי בלבד (monkeypatch מחזיר אוטומטית)."""
    monkeypatch.setenv("TENANCY_STRICT", "true")


class TestStrictEnforcement:
    def test_get_connection_without_context_raises(self, strict, tmp_path):
        """קו ההגנה: גישת DB בלי context נכשלת רועש תחת strict."""
        with patch("ai_chatbot.config.DB_PATH", tmp_path / "d.db"):
            from database import get_connection
            with pytest.raises(MissingTenantContext):
                with get_connection():
                    pass  # pragma: no cover

    def test_get_connection_with_default_context_works(self, strict, tmp_path):
        with patch("ai_chatbot.config.DB_PATH", tmp_path / "d.db"):
            from database import get_connection, init_db
            with tenant_context(DEFAULT_TENANT):
                init_db()
                with get_connection() as c:
                    assert c.execute("SELECT 1").fetchone()[0] == 1


class TestBootUnderStrict:
    """נתיב האתחול של main.py (python -m main ב-Render) חייב לעבוד תחת strict."""

    def test_boot_prologue_works_under_strict(self, strict, tmp_path):
        """מדמה את בלוק האתחול של main() — init_db + seed + count — תחת
        strict, כשהוא עטוף ב-tenant_context(DEFAULT_TENANT) כמו בקוד."""
        with patch("ai_chatbot.config.DATA_DIR", tmp_path), \
             patch("ai_chatbot.config.DB_PATH", tmp_path / "default.db"), \
             patch("ai_chatbot.config.FAISS_INDEX_PATH", tmp_path / "faiss"):
            from ai_chatbot import database as db
            from seed_data import _seed_business_hours
            with tenant_context(DEFAULT_TENANT):
                db.init_db()
                _seed_business_hours()
                assert db.count_kb_entries(active_only=False) == 0

    def test_boot_without_wrap_would_have_crashed_under_strict(self, strict, tmp_path):
        """הוכחה שה-wrap ב-main.py הכרחי: בדיוק מה ש-main עשה קודם
        (db.init_db בלי context) היה קורס תחת strict."""
        with patch("ai_chatbot.config.DB_PATH", tmp_path / "default.db"):
            from ai_chatbot import database as db
            with pytest.raises(MissingTenantContext):
                db.init_db()


class TestFlaskUnderStrict:
    """ה-Flask admin (WSGI/entry) מגיש בקשות תחת strict — before_request
    קובע context (default כשאין session), teardown מאפס."""

    def _app(self):
        import admin.app as aa
        with patch.object(aa, "ADMIN_SECRET_KEY", "s"), \
             patch.object(aa, "ADMIN_USERNAME", "admin"), \
             patch.object(aa, "ADMIN_PASSWORD", "pw"):
            app = aa.create_admin_app()
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        return app

    def test_login_page_served_under_strict(self, strict, tmp_path):
        with patch("ai_chatbot.config.DATA_DIR", tmp_path), \
             patch("ai_chatbot.config.DB_PATH", tmp_path / "default.db"):
            from ai_chatbot import database as db
            with tenant_context(DEFAULT_TENANT):
                db.init_db()
            r = self._app().test_client().get("/login")
            assert r.status_code == 200

    def test_authenticated_db_route_under_strict(self, strict, tmp_path):
        """בקשה מאומתת שנוגעת ב-DB (dashboard) עוברת תחת strict —
        before_request קבע את ה-context לפני שה-route רץ."""
        with patch("ai_chatbot.config.DATA_DIR", tmp_path), \
             patch("ai_chatbot.config.DB_PATH", tmp_path / "default.db"):
            from ai_chatbot import database as db
            with tenant_context(DEFAULT_TENANT):
                db.init_db()
            client = self._app().test_client()
            with client.session_transaction() as s:
                s["logged_in"] = True
            r = client.get("/", follow_redirects=False)
            assert r.status_code in (200, 302)
