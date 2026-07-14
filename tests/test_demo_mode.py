"""
טסטים למצב הדמו (docs/demo-mode-spec.md V3).

בודק את שלוש שכבות ההגנה:
1. session flag + auto-login דרך /demo
2. middleware שחוסם POST/PUT/PATCH/DELETE מ-session דמו
3. stubs ב-DEMO_MODE ליציאות חיצוניות (Twilio, broadcast)

וגם אי-רגרסיה — אדמין רגיל ממשיך לעבוד.
"""

import importlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─── Helpers ────────────────────────────────────────────────────────────


def _build_app(monkeypatch, tmp_path, *, demo_mode: bool = True):
    """
    בונה Flask test app טרי, עם DB זמני ו-DEMO_MODE מבוקר.
    מחזיר test_client.
    """
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "adminpass123")
    monkeypatch.setenv("ADMIN_SECRET_KEY", "test-secret")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DEVELOPER_PASSWORD", "")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "")
    monkeypatch.setenv("TWILIO_WHATSAPP_NUMBER", "")
    monkeypatch.setenv("DEMO_MODE", "true" if demo_mode else "false")
    monkeypatch.setenv("DEMO_CTA_WHATSAPP", "+972501234567")
    monkeypatch.setenv("DEMO_LIVE_BOT_URL", "https://t.me/demo_bot")

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


def _login_as_demo(client):
    """כניסה דרך /demo — יוצרת session מסומנת."""
    resp = client.get("/demo", follow_redirects=False)
    assert resp.status_code == 302, "GET /demo צריך להחזיר 302"
    return resp


def _login_as_admin(client):
    """כניסת אדמין רגילה (לא דמו)."""
    resp = client.post(
        "/login",
        data={"username": "admin", "password": "adminpass123"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    return resp


# ─── א. /demo entry ──────────────────────────────────────────────────────


class TestDemoEntry:
    """ה-route /demo — auto-login לגולש דמו."""

    def test_demo_entry_when_enabled(self, tmp_path, monkeypatch):
        client = _build_app(monkeypatch, tmp_path, demo_mode=True)
        resp = client.get("/demo", follow_redirects=False)
        assert resp.status_code == 302
        with client.session_transaction() as sess:
            assert sess.get("logged_in") is True
            assert sess.get("demo") is True

    def test_demo_entry_404_when_disabled(self, tmp_path, monkeypatch):
        """כשה-DEMO_MODE כבוי — הראוט בכלל לא קיים (404)."""
        client = _build_app(monkeypatch, tmp_path, demo_mode=False)
        resp = client.get("/demo", follow_redirects=False)
        assert resp.status_code == 404

    def test_demo_entry_clears_previous_session(self, tmp_path, monkeypatch):
        """
        גם אם הייתה כבר session של אדמין רגיל — /demo מוחקת אותה ויוצרת
        חדשה. מונע מצב שאדמין רגיל מתחבר בטעות דרך /demo ושומר הרשאות.
        """
        client = _build_app(monkeypatch, tmp_path, demo_mode=True)
        _login_as_admin(client)
        with client.session_transaction() as sess:
            assert sess.get("logged_in") is True
            assert sess.get("demo") is None

        _login_as_demo(client)
        with client.session_transaction() as sess:
            assert sess.get("demo") is True


# ─── ב. middleware של read-only ─────────────────────────────────────────


class TestDemoReadOnlyMiddleware:
    """ה-middleware _enforce_demo_readonly — חסימת כתיבות מ-session דמו."""

    def test_get_allowed(self, tmp_path, monkeypatch):
        client = _build_app(monkeypatch, tmp_path)
        _login_as_demo(client)
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 200

    def test_head_allowed(self, tmp_path, monkeypatch):
        """HEAD היא שיטה בטוחה — Flask מנתב אוטומטית ל-handler של GET."""
        client = _build_app(monkeypatch, tmp_path)
        _login_as_demo(client)
        resp = client.head("/")
        # HEAD על / לא אמור להיחסם — דמו או לא.
        assert resp.status_code in (200, 302)

    def test_post_blocked_non_htmx(self, tmp_path, monkeypatch):
        """POST רגיל (לא-HTMX) → flash + redirect חזרה לעמוד המקור."""
        client = _build_app(monkeypatch, tmp_path)
        _login_as_demo(client)
        resp = client.post(
            "/kb/add",
            data={"category": "general", "title": "t", "content": "test"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        # לוודא שהרשומה *לא* נכנסה ל-DB.
        import database
        entries = database.get_all_kb_entries()
        assert len(entries) == 0

    def test_post_blocked_htmx(self, tmp_path, monkeypatch):
        """POST מ-HTMX → 200 עם fragment של toast + HX-Retarget."""
        client = _build_app(monkeypatch, tmp_path)
        _login_as_demo(client)
        resp = client.post(
            "/kb/add",
            data={"category": "general", "title": "t", "content": "test"},
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert resp.headers.get("HX-Retarget") == "#demo-toast-container"
        assert resp.headers.get("HX-Reswap") == "innerHTML"
        # ה-body צריך להכיל את ה-toast (טקסט בעברית).
        body = resp.get_data(as_text=True)
        assert "דמו" in body or "demo" in body.lower()

    def test_delete_blocked(self, tmp_path, monkeypatch):
        """DELETE — שיטה לא-בטוחה, חסומה."""
        client = _build_app(monkeypatch, tmp_path)
        _login_as_demo(client)
        # /kb/delete/<id> מקבל POST בקוד; משתמשים ב-PATCH לראוט קיים.
        resp = client.delete("/kb/delete/1")
        # 405 (Method Not Allowed) מותר — העיקר לא 200 בלי toast.
        # אם נחסם — נקבל 302 (flash+redirect, לא-HTMX).
        assert resp.status_code in (302, 405)

    def test_logout_allowed(self, tmp_path, monkeypatch):
        """logout חייב לעבוד גם ב-session דמו (allowlist)."""
        client = _build_app(monkeypatch, tmp_path)
        _login_as_demo(client)
        resp = client.get("/logout", follow_redirects=False)
        assert resp.status_code == 302
        # ה-session נוקתה.
        with client.session_transaction() as sess:
            assert sess.get("demo") is None
            assert sess.get("logged_in") is None


# ─── ג. /dev/* חסום לגולש דמו ──────────────────────────────────────────


class TestDemoDevRoutesBlocked:
    """גולש דמו לא יכול לגעת באזור המפתח."""

    def test_dev_login_blocked_for_demo(self, tmp_path, monkeypatch):
        """
        /dev/login — גם כש-DEVELOPER_PASSWORD ריק (=404 רגיל), וגם בגישה
        מ-session דמו (=redirect ל-dashboard). מקבלים אחד מהשניים, לא 200.
        """
        client = _build_app(monkeypatch, tmp_path)
        _login_as_demo(client)
        resp = client.get("/dev/login", follow_redirects=False)
        assert resp.status_code in (302, 404)

    def test_dev_subscription_blocked_for_demo(self, tmp_path, monkeypatch):
        client = _build_app(monkeypatch, tmp_path)
        _login_as_demo(client)
        resp = client.get("/dev/subscription", follow_redirects=False)
        assert resp.status_code in (302, 404)


# ─── ד. אדמין רגיל לא מושפע ─────────────────────────────────────────────


class TestNormalAdminUnaffected:
    """אסור שמצב הדמו ישבור login רגיל של אדמין."""

    def test_normal_admin_can_post(self, tmp_path, monkeypatch):
        """אדמין שמתחבר ב-/login יכול לכתוב ב-DB כרגיל."""
        client = _build_app(monkeypatch, tmp_path)
        _login_as_admin(client)
        # POST שאמור להצליח (לא דרך session דמו).
        resp = client.post(
            "/kb/add",
            data={"category": "general", "title": "t", "content": "test entry"},
            follow_redirects=False,
        )
        # 302 אחרי הצלחה (redirect ל-/kb).
        assert resp.status_code in (200, 302)

    def test_normal_admin_no_demo_banner(self, tmp_path, monkeypatch):
        """אדמין רגיל לא רואה את ה-banner של מצב הדמו."""
        client = _build_app(monkeypatch, tmp_path)
        _login_as_admin(client)
        resp = client.get("/")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "demo-banner" not in body
        assert "מצב דמו" not in body

    def test_demo_banner_visible_in_session(self, tmp_path, monkeypatch):
        """גולש דמו רואה את ה-banner."""
        client = _build_app(monkeypatch, tmp_path)
        _login_as_demo(client)
        resp = client.get("/")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "demo-banner" in body
        assert "מצב דמו" in body


# ─── ה. stub ל-WhatsApp ─────────────────────────────────────────────────


class TestWhatsappStub:
    """send_whatsapp צריך לדלג על Twilio ב-DEMO_MODE=true."""

    def test_whatsapp_skips_in_demo_mode(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEMO_MODE", "true")
        monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC_dummy")
        monkeypatch.setenv("TWILIO_AUTH_TOKEN", "dummy")
        monkeypatch.setenv("TWILIO_WHATSAPP_NUMBER", "+15550000000")

        import config as _root_config
        importlib.reload(_root_config)
        import ai_chatbot.config
        importlib.reload(ai_chatbot.config)
        import messaging.whatsapp_sender as ws
        importlib.reload(ws)

        # mock על _get_twilio_client — אם הוא נקרא, הטסט נכשל.
        fake_client = MagicMock()
        with patch.object(ws, "_get_twilio_client", return_value=fake_client):
            ws.send_whatsapp("+972501234567", "hello demo")

        # ב-DEMO_MODE — לא אמורים להגיע ל-twilio בכלל.
        assert fake_client.messages.create.call_count == 0

    def test_whatsapp_sends_when_disabled(self, tmp_path, monkeypatch):
        """כש-DEMO_MODE כבוי — send_whatsapp כן קורא ל-Twilio."""
        monkeypatch.setenv("DEMO_MODE", "false")
        monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC_dummy")
        monkeypatch.setenv("TWILIO_AUTH_TOKEN", "dummy")
        monkeypatch.setenv("TWILIO_WHATSAPP_NUMBER", "+15550000000")

        import config as _root_config
        importlib.reload(_root_config)
        import ai_chatbot.config
        importlib.reload(ai_chatbot.config)
        import messaging.whatsapp_sender as ws
        importlib.reload(ws)

        fake_client = MagicMock()
        with patch.object(ws, "_get_twilio_client", return_value=fake_client):
            ws.send_whatsapp("+972501234567", "real send")

        assert fake_client.messages.create.call_count == 1


# ─── ו. stub ל-broadcast ────────────────────────────────────────────────


class TestBroadcastStub:
    """send_broadcast צריך לדלג על שליחה בפועל ב-DEMO_MODE=true."""

    @pytest.fixture
    def db_with_demo(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEMO_MODE", "true")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "bc_demo.db"))
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import config as _root_config
        importlib.reload(_root_config)
        import ai_chatbot.config
        importlib.reload(ai_chatbot.config)
        import database
        importlib.reload(database)
        database.init_db()
        # feature gate — מאפשרים broadcast כדי שהבדיקה תגיע ל-stub של דמו.
        import feature_flags
        importlib.reload(feature_flags)
        feature_flags.override_feature("broadcast", True)
        import broadcast_service
        importlib.reload(broadcast_service)
        return database

    @pytest.mark.asyncio
    async def test_broadcast_skips_in_demo_mode(self, db_with_demo):
        """ב-DEMO_MODE: לא קוראים ל-bot.send_message ולא ל-send_whatsapp."""
        from broadcast_service import send_broadcast

        bot = AsyncMock()
        bot.send_message = AsyncMock()

        bc_id = db_with_demo.create_broadcast("שלום דמו", "all", 3)
        await send_broadcast(bot, bc_id, "שלום דמו", ["111", "222", "333"])

        # אף שליחה אמיתית לא בוצעה.
        assert bot.send_message.call_count == 0
        # השידור סומן כ-completed עם sent=3, failed=0.
        broadcasts = db_with_demo.get_all_broadcasts()
        target = next(b for b in broadcasts if b["id"] == bc_id)
        assert target["status"] == "completed"
        assert target["sent_count"] == 3
        assert target["failed_count"] == 0
