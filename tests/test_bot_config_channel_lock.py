"""
טסטים לנעילת ערוץ ב-/bot-config — לפי ערוץ ה-tenant, לא לפי החבילה.

הכללים (אחרי ניתוק הערוץ מהחבילות):
- ה-tenant של ברירת המחדל (legacy, env) — **לעולם לא נעול**, בכל חבילה.
  (בעבר premium נעל את מקטע הטלגרם — זה היה הבאג שדווח.)
- tenant בפלטפורמה: ערוץ ריק ⇒ שני המקטעים פתוחים; ערוץ שנקבע (בחיבור
  הראשון) ⇒ המקטע השני נעול בתבנית וגם ב-POST ישיר (curl/HTMX).
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
    # החבילה כבר לא קובעת ערוץ — נקבעת רק כדי להוכיח שאין לה השפעה על הנעילה
    feature_flags.set_plan(plan, reason="test")
    import control_plane as _cp
    _cp.invalidate_status_cache()
    import admin.app as _admin_app
    importlib.reload(_admin_app)

    from admin.app import create_admin_app
    app = create_admin_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    return app.test_client()


def _login(client):
    client.post("/login", data={"username": "admin", "password": "adminpass123"})


def _setup_locked_tenant(slug: str, channel: str):
    """יצירת tenant בפלטפורמה עם ערוץ נעול (מדמה חיבור ראשון שהצליח)."""
    import control_plane as cp
    import feature_flags
    from tenancy import tenant_context

    cp.create_tenant(slug, "עסק לנעילה")
    if channel:
        with tenant_context(slug):
            feature_flags.set_channel(channel)


def _act_as(client, slug: str):
    """session של platform admin במצב פעל-כ (בלי מסלול login מלא)."""
    with client.session_transaction() as sess:
        sess["logged_in"] = True
        sess["admin_email"] = "p@platform.example"
        sess["admin_role"] = "platform_admin"
        sess["acting_tenant"] = slug


def _input_tag(body: str, element_id: str) -> str:
    idx = body.find(f'id="{element_id}"')
    assert idx > 0, f"לא נמצא input עם id={element_id}"
    end = body.find(">", idx)
    return body[idx:end]


# ── תבנית: הנעילה נגזרת מערוץ ה-tenant ─────────────────────────────────


class TestBotConfigUILocking:
    @pytest.mark.parametrize("plan", ["basic", "advanced", "premium"])
    def test_default_tenant_never_locked(self, tmp_path, monkeypatch, plan):
        """ה-tenant של ברירת המחדל פתוח בשני הערוצים — בכל חבילה.

        רגרסיה לבאג שדווח: premium (שהיה ממופה ל-WhatsApp) נעל את מקטע
        הטלגרם גם כשהעסק עובד על טלגרם.
        """
        client = _build_app(monkeypatch, tmp_path, plan=plan)
        _login(client)
        body = client.get("/bot-config").data.decode("utf-8")
        assert "channel-card-disabled" not in body
        assert "disabled" not in _input_tag(body, "telegram_bot_token")
        assert "disabled" not in _input_tag(body, "twilio_account_sid")

    def test_platform_tenant_unset_channel_both_open(self, tmp_path, monkeypatch):
        client = _build_app(monkeypatch, tmp_path)
        _setup_locked_tenant("fresh-biz", channel="")
        _act_as(client, "fresh-biz")
        body = client.get("/bot-config").data.decode("utf-8")
        assert "channel-card-disabled" not in body
        # הסבר מנגנון הנעילה מוצג כשעדיין אין ערוץ
        assert "יינעל אוטומטית" in body

    def test_platform_tenant_whatsapp_channel_locks_telegram(self, tmp_path, monkeypatch):
        client = _build_app(monkeypatch, tmp_path)
        _setup_locked_tenant("wa-biz", channel="whatsapp")
        _act_as(client, "wa-biz")
        body = client.get("/bot-config").data.decode("utf-8")
        assert "channel-card-disabled" in body
        assert "disabled" in _input_tag(body, "telegram_bot_token")
        assert "disabled" not in _input_tag(body, "twilio_account_sid")

    def test_platform_tenant_telegram_channel_locks_whatsapp(self, tmp_path, monkeypatch):
        client = _build_app(monkeypatch, tmp_path)
        _setup_locked_tenant("tg-biz", channel="telegram")
        _act_as(client, "tg-biz")
        body = client.get("/bot-config").data.decode("utf-8")
        assert "disabled" in _input_tag(body, "twilio_account_sid")
        assert "disabled" not in _input_tag(body, "telegram_bot_token")


# ── Backend: הנעילה נאכפת גם ב-POST ישיר (curl) ────────────────────────


class TestBotConfigBackendLocking:
    def test_default_premium_allows_telegram_post(self, tmp_path, monkeypatch):
        """הבאג המקורי: premium חסם את שמירת הטלגרם ב-default. עכשיו עובר."""
        client = _build_app(monkeypatch, tmp_path, plan="premium")
        _login(client)
        resp = client.post(
            "/bot-config",
            data={
                "form_type": "telegram",
                "telegram_bot_token": "test-new-token",
                "telegram_owner_chat_id": "987654321",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302
        import ai_chatbot.config as _cfg
        assert _cfg.TELEGRAM_BOT_TOKEN == "test-new-token"

    def test_default_basic_allows_whatsapp_post(self, tmp_path, monkeypatch):
        """הצד השני של אותו באג: basic חסם את שמירת ה-WhatsApp ב-default."""
        client = _build_app(monkeypatch, tmp_path, plan="basic")
        _login(client)
        resp = client.post(
            "/bot-config",
            data={
                "form_type": "whatsapp",
                "twilio_account_sid": "AC" + "f" * 32,
                "twilio_auth_token": "e" * 32,
                "twilio_whatsapp_number": "+14155559999",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302
        import ai_chatbot.config as _cfg
        assert _cfg.TWILIO_ACCOUNT_SID == "AC" + "f" * 32

    def test_platform_tenant_locked_channel_blocks_other_post(self, tmp_path, monkeypatch):
        """tenant שערוצו נעול ל-WhatsApp — POST טלגרם נדחה ולא נשמר דבר."""
        client = _build_app(monkeypatch, tmp_path)
        _setup_locked_tenant("wa-biz2", channel="whatsapp")
        _act_as(client, "wa-biz2")
        resp = client.post(
            "/bot-config",
            data={
                "form_type": "telegram",
                "telegram_bot_token": "tampered-token",
                "telegram_owner_chat_id": "111",
            },
            follow_redirects=True,
        )
        assert "נעולות" in resp.get_data(as_text=True)
        import control_plane as cp
        assert cp.get_tenant_secret("wa-biz2", "telegram_bot_token") is None
