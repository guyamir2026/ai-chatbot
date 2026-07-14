"""
טסטים לפיצ'ר הודעת הפתיחה המשפטית (implied consent) ולעמוד המסמכים
הציבורי (/legal).

שלוש קבוצות:
- build_intro_disclaimer: נוסח ההודעה, הזרקת שם + קישור, escaping ב-HTML,
  ו-fallback בלי ADMIN_URL.
- disclaimer_sent / mark_disclaimer_sent: דגל ה"שלח פעם אחת" ב-DB.
- /legal: העמוד הציבורי מגיש terms/privacy, מזריק שם עסק, 404 למסמך
  לא-קיים.
"""

from __future__ import annotations

import pytest
from unittest.mock import patch


# ────────────────────────────────────────────────────────────────────
# build_intro_disclaimer
# ────────────────────────────────────────────────────────────────────


class TestBuildIntroDisclaimer:
    def test_core_text_present(self):
        import config
        msg = config.build_intro_disclaimer(html_link=False)
        assert "המשך השיחה מהווה אישור" in msg
        assert "איך אפשר לעזור" in msg
        assert "הסוכן החכם" in msg

    def test_fallback_without_admin_url(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "ADMIN_URL", "")
        msg = config.build_intro_disclaimer(html_link=False)
        # בלי ADMIN_URL — נוסח חלופי בלי קישור, ולא נשבר
        assert "אצל בעל העסק" in msg
        assert "http" not in msg

    def test_whatsapp_raw_url(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "ADMIN_URL", "https://bot.example.com")
        msg = config.build_intro_disclaimer(html_link=False)
        assert "https://bot.example.com/legal" in msg
        # ווטסאפ — URL גולמי, בלי תגיות HTML
        assert "<a" not in msg

    def test_telegram_html_link(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "ADMIN_URL", "https://bot.example.com")
        msg = config.build_intro_disclaimer(html_link=True)
        assert '<a href="https://bot.example.com/legal">' in msg

    def test_telegram_escapes_business_name(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "BUSINESS_NAME", "Bob's & Co <Salon>")
        msg = config.build_intro_disclaimer(html_link=True)
        # תווים מיוחדים ב-HTML חייבים escaping (מניעת שבירת parse_mode=HTML)
        assert "Bob's & Co <Salon>" not in msg
        assert "&amp;" in msg and "&lt;" in msg


# ────────────────────────────────────────────────────────────────────
# disclaimer_sent / mark_disclaimer_sent
# ────────────────────────────────────────────────────────────────────


class TestDisclaimerSentFlag:
    def test_default_false_for_unknown(self, db_conn):
        from database import disclaimer_sent
        assert disclaimer_sent("u_unknown") is False

    def test_mark_then_true(self, db_conn):
        from database import disclaimer_sent, mark_disclaimer_sent, upsert_user
        upsert_user("u1", "name", channel="telegram")
        assert disclaimer_sent("u1") is False
        mark_disclaimer_sent("u1")
        assert disclaimer_sent("u1") is True

    def test_mark_idempotent_keeps_first_timestamp(self, db_conn):
        from database import mark_disclaimer_sent, upsert_user, get_connection
        upsert_user("u1", "name", channel="telegram")
        mark_disclaimer_sent("u1")
        with get_connection() as conn:
            first = conn.execute(
                "SELECT disclaimer_sent_at FROM users WHERE user_id = ?", ("u1",),
            ).fetchone()["disclaimer_sent_at"]
        # קריאה חוזרת לא דורסת (COALESCE)
        mark_disclaimer_sent("u1")
        with get_connection() as conn:
            second = conn.execute(
                "SELECT disclaimer_sent_at FROM users WHERE user_id = ?", ("u1",),
            ).fetchone()["disclaimer_sent_at"]
        assert first == second

    def test_mark_missing_row_is_noop(self, db_conn):
        # UPDATE-only: אם השורה לא קיימת, לא נוצרת שורה ולא נזרקת חריגה
        from database import disclaimer_sent, mark_disclaimer_sent
        mark_disclaimer_sent("u_missing")
        assert disclaimer_sent("u_missing") is False

    def test_deleted_with_user(self, db_conn):
        # מחיקת המשתמש (זכות מחיקה) מוחקת גם את הדגל — שורת users נמחקת
        from database import (
            disclaimer_sent, mark_disclaimer_sent, upsert_user, delete_user_data,
        )
        upsert_user("u_del", "name", channel="telegram")
        mark_disclaimer_sent("u_del")
        assert disclaimer_sent("u_del") is True
        delete_user_data("u_del")
        assert disclaimer_sent("u_del") is False


# ────────────────────────────────────────────────────────────────────
# /legal — עמוד ציבורי
# ────────────────────────────────────────────────────────────────────


@pytest.fixture
def _admin_env(monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", "test_admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "test_pass_for_unit_tests_only")
    monkeypatch.setenv("ADMIN_SECRET_KEY", "test-secret-key-for-unit-tests")
    import importlib
    for mod_name in ("ai_chatbot.config", "admin.app"):
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            continue
        for attr, val in (
            ("ADMIN_USERNAME", "test_admin"),
            ("ADMIN_PASSWORD", "test_pass_for_unit_tests_only"),
            ("ADMIN_SECRET_KEY", "test-secret-key-for-unit-tests"),
        ):
            if hasattr(mod, attr):
                monkeypatch.setattr(mod, attr, val, raising=False)


@pytest.fixture
def client(db_conn, _admin_env):
    from admin.app import create_admin_app
    app = create_admin_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SECRET_KEY"] = "test-secret"
    with app.test_client() as c:
        yield c  # בלי login — /legal ציבורי


class TestLegalRoutes:
    def test_index_shows_both_docs(self, client):
        resp = client.get("/legal")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert "תנאי שימוש" in body
        assert "מדיניות פרטיות" in body

    def test_terms_rendered_and_name_injected(self, client):
        resp = client.get("/legal/terms")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert "תנאי שימוש" in body
        # ה-placeholder הוחלף בשם העסק (get_business_config)
        assert "[שם בעל העסק]" not in body

    def test_privacy_rendered(self, client):
        resp = client.get("/legal/privacy")
        assert resp.status_code == 200
        assert "מדיניות פרטיות" in resp.data.decode("utf-8")

    def test_unknown_doc_404(self, client):
        resp = client.get("/legal/bogus")
        assert resp.status_code == 404

    def test_noindex_meta(self, client):
        # המסמכים לא צריכים להיאנדקס במנועי חיפוש
        resp = client.get("/legal/terms")
        assert "noindex" in resp.data.decode("utf-8").lower()


# ────────────────────────────────────────────────────────────────────
# WhatsApp — הודעת פתיחה לפונה ראשון
# ────────────────────────────────────────────────────────────────────


class TestWhatsappFirstContact:
    def test_first_contact_body_is_disclaimer(self, db_conn, monkeypatch):
        import messaging.whatsapp_webhook as ww

        captured = {}

        def _fake_ensure(friendly_name, body, buttons):
            captured["name"] = friendly_name
            captured["body"] = body
            return "sid_test"

        monkeypatch.setattr(
            "messaging.whatsapp_templates.ensure_quick_reply", _fake_ensure,
        )
        monkeypatch.setattr(
            "messaging.whatsapp_templates.send_with_template", lambda *a, **k: None,
        )

        ww._send_welcome_message("+972500000000", is_first_contact=True)

        assert "המשך השיחה מהווה אישור" in captured["body"]
        # template נפרד לפונה ראשון — כדי לא להתנגש ב-cache של welcome רגיל
        assert captured["name"].startswith("welcome_menu_intro")

    def test_regular_welcome_not_disclaimer(self, db_conn, monkeypatch):
        import messaging.whatsapp_webhook as ww

        captured = {}
        monkeypatch.setattr(
            "messaging.whatsapp_templates.ensure_quick_reply",
            lambda friendly_name, body, buttons: captured.update(
                name=friendly_name, body=body) or "sid",
        )
        monkeypatch.setattr(
            "messaging.whatsapp_templates.send_with_template", lambda *a, **k: None,
        )

        ww._send_welcome_message("+972500000001", is_first_contact=False)

        assert "המשך השיחה מהווה אישור" not in captured["body"]
        assert "welcome_menu_intro" not in captured["name"]
