"""
טסטים לאכיפת Feature Flags (Phase 3) — שכבת backend services + before_request.

הטסטים האלה מוודאים שאם לקוח על basic מנסה להפעיל פיצ'ר Premium (broadcast,
followup_24h), השירות עוצר אותו לפני שהוא עושה משהו אמיתי.
"""

import importlib
from unittest.mock import patch, AsyncMock, MagicMock

import pytest


@pytest.fixture
def db_basic(tmp_path):
    """DB טרי עם חבילת basic (ברירת המחדל אחרי init_db)."""
    db_path = tmp_path / "test.db"
    with patch("ai_chatbot.config.DB_PATH", db_path):
        import database
        importlib.reload(database)
        database.init_db()
        import feature_flags
        importlib.reload(feature_flags)
        # ברירת המחדל היא premium (מוצר חד-שכבתי) — קובעים basic מפורשות
        # כדי לבדוק את חסימות הפיצ'רים.
        feature_flags.set_plan("basic", reason="test fixture")
        assert feature_flags.get_current_plan() == "basic"
        assert feature_flags.has_feature("broadcast") is False
        assert feature_flags.has_feature("followup_24h") is False
        yield database


@pytest.fixture
def db_premium(tmp_path):
    """DB טרי עם premium מופעל ידנית."""
    db_path = tmp_path / "test.db"
    with patch("ai_chatbot.config.DB_PATH", db_path):
        import database
        importlib.reload(database)
        database.init_db()
        import feature_flags
        importlib.reload(feature_flags)
        feature_flags.set_plan("premium", reason="test fixture")
        assert feature_flags.has_feature("broadcast") is True
        assert feature_flags.has_feature("followup_24h") is True
        yield database


@pytest.fixture
def mock_bot():
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    bot.initialize = AsyncMock()
    bot.shutdown = AsyncMock()
    return bot


# ── send_broadcast: אכיפה ב-basic ──────────────────────────────────────────


class TestBroadcastBlockedInBasic:
    @pytest.mark.asyncio
    async def test_send_broadcast_blocked_in_basic(self, db_basic, mock_bot):
        """ב-basic, send_broadcast מסומן fail מיד ולא שולח כלום."""
        from broadcast_service import send_broadcast

        bc_id = db_basic.create_broadcast("שלום", "all", 3)
        await send_broadcast(mock_bot, bc_id, "שלום", ["111", "222", "333"])

        mock_bot.send_message.assert_not_called()
        # הסטטוס סומן 'failed' ב-DB
        with db_basic.get_connection() as conn:
            row = conn.execute(
                "SELECT status FROM broadcast_messages WHERE id = ?", (bc_id,)
            ).fetchone()
        assert row["status"] == "failed"

    @pytest.mark.asyncio
    async def test_send_broadcast_works_in_premium(self, db_premium, mock_bot):
        """ב-premium, send_broadcast עובר ושולח."""
        from broadcast_service import send_broadcast

        bc_id = db_premium.create_broadcast("שלום", "all", 1)
        await send_broadcast(mock_bot, bc_id, "שלום", ["111"])

        mock_bot.send_message.assert_called_once()


# ── analyze_lead / process_pending_followups: אכיפה ב-basic ─────────────────


class TestFollowupBlockedInBasic:
    def test_analyze_lead_skipped_in_basic(self, db_basic, monkeypatch):
        # forces FOLLOWUP_ENABLED = True כדי לבדוק רק את ה-feature gate
        monkeypatch.setattr("followup_service.FOLLOWUP_ENABLED", True)

        # mock _analyze_lead_inner — אם הוא נקרא, הטסט נכשל
        called = {"yes": False}

        def _fake_inner(*args, **kwargs):
            called["yes"] = True

        monkeypatch.setattr("followup_service._analyze_lead_inner", _fake_inner)

        from followup_service import analyze_lead
        analyze_lead("user-1", username="x", channel="telegram")
        assert called["yes"] is False, (
            "analyze_lead לא היה אמור לרוץ בחבילת basic"
        )

    def test_analyze_lead_runs_in_premium(self, db_premium, monkeypatch):
        monkeypatch.setattr("followup_service.FOLLOWUP_ENABLED", True)

        called = {"yes": False}

        def _fake_inner(*args, **kwargs):
            called["yes"] = True

        monkeypatch.setattr("followup_service._analyze_lead_inner", _fake_inner)

        from followup_service import analyze_lead
        analyze_lead("user-1", username="x", channel="telegram")
        assert called["yes"] is True

    @pytest.mark.asyncio
    async def test_process_pending_followups_skipped_in_basic(
        self, db_basic, monkeypatch
    ):
        monkeypatch.setattr("followup_service.FOLLOWUP_ENABLED", True)

        from followup_service import process_pending_followups
        result = await process_pending_followups()
        assert result == {"processed": 0, "sent": 0, "skipped": 0, "errors": 0}


# ── before_request feature gate ב-Flask ────────────────────────────────────


def _build_app(monkeypatch, tmp_path, *, plan: str = "basic"):
    """app טרי עם plan נתון."""
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "adminpass123")
    monkeypatch.setenv("ADMIN_SECRET_KEY", "test-secret")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DEVELOPER_PASSWORD", "devpass-secret-123")
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
    # ברירת המחדל היא premium; קובעים מפורשות (כולל basic) לבדיקת ה-gate.
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


class TestFeatureGateRoutes:
    def test_broadcast_redirects_in_basic(self, tmp_path, monkeypatch):
        client = _build_app(monkeypatch, tmp_path, plan="basic")
        _login(client)
        resp = client.get("/broadcast", follow_redirects=False)
        # נחסם — redirect ל-dashboard
        assert resp.status_code == 302
        assert resp.headers.get("Location", "").endswith("/")

    def test_broadcast_send_blocked_in_basic(self, tmp_path, monkeypatch):
        client = _build_app(monkeypatch, tmp_path, plan="basic")
        _login(client)
        resp = client.post(
            "/broadcast/send",
            data={"audience": "all", "message": "hi"},
            follow_redirects=False,
        )
        # POST גם נחסם
        assert resp.status_code == 302

    def test_broadcast_works_in_premium(self, tmp_path, monkeypatch):
        client = _build_app(monkeypatch, tmp_path, plan="premium")
        _login(client)
        resp = client.get("/broadcast", follow_redirects=False)
        # premium → broadcast מותר → 200
        assert resp.status_code == 200

    def test_followups_blocked_in_basic(self, tmp_path, monkeypatch):
        client = _build_app(monkeypatch, tmp_path, plan="basic")
        _login(client)
        resp = client.get("/followups", follow_redirects=False)
        assert resp.status_code == 302

    def test_followups_allowed_in_advanced(self, tmp_path, monkeypatch):
        client = _build_app(monkeypatch, tmp_path, plan="advanced")
        _login(client)
        resp = client.get("/followups", follow_redirects=False)
        # advanced כולל followup_24h → 200
        assert resp.status_code == 200

    def test_dev_panel_not_blocked_by_feature_gate(self, tmp_path, monkeypatch):
        """איזור /dev/* תמיד עובר — המפתח לא נחסם בגלל חבילה."""
        client = _build_app(monkeypatch, tmp_path, plan="basic")
        # ללא login admin, אבל עם dev login
        client.post("/dev/login", data={"password": "devpass-secret-123"})
        resp = client.get("/dev/subscription", follow_redirects=False)
        assert resp.status_code == 200

    def test_dashboard_not_blocked(self, tmp_path, monkeypatch):
        """דשבורד הוא לא feature-gated — תמיד נגיש."""
        client = _build_app(monkeypatch, tmp_path, plan="basic")
        _login(client)
        resp = client.get("/", follow_redirects=False)
        # dashboard לא תחת broadcast/followups → 200
        assert resp.status_code == 200

    def test_htmx_request_returns_403_with_trigger(self, tmp_path, monkeypatch):
        """HTMX מקבל 403 עם HX-Trigger במקום redirect (לא ידרוס תוכן ב-DOM)."""
        client = _build_app(monkeypatch, tmp_path, plan="basic")
        _login(client)
        resp = client.get(
            "/broadcast",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 403
        assert resp.headers.get("HX-Trigger") == "featureDenied"
        assert resp.headers.get("HX-Reswap") == "none"


# ── developer_alerts ────────────────────────────────────────────────────────


class TestDeveloperAlerts:
    def test_notify_skipped_when_chat_id_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
        monkeypatch.setenv("DEVELOPER_TELEGRAM_CHAT_ID", "")
        import config as _root_config
        importlib.reload(_root_config)
        import ai_chatbot.config as _cfg
        importlib.reload(_cfg)

        import developer_alerts
        importlib.reload(developer_alerts)

        # אין chat_id → מחזיר False, לא זורק
        sent = developer_alerts.notify_developer("test message")
        assert sent is False

    def test_notify_attempts_send_when_chat_id_set(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
        monkeypatch.setenv("DEVELOPER_TELEGRAM_CHAT_ID", "987654321")
        monkeypatch.setenv("DEPLOYMENT_NAME", "test-deployment")
        import config as _root_config
        importlib.reload(_root_config)
        import ai_chatbot.config as _cfg
        importlib.reload(_cfg)

        import developer_alerts
        importlib.reload(developer_alerts)

        sent_calls = []

        def _fake_send(chat_id, text):
            sent_calls.append((chat_id, text))
            return True

        monkeypatch.setattr(developer_alerts, "_send_telegram", _fake_send)

        ok = developer_alerts.notify_developer("hello world")
        assert ok is True
        assert len(sent_calls) == 1
        chat_id, text = sent_calls[0]
        assert chat_id == "987654321"
        assert "test-deployment" in text
        assert "hello world" in text

    def test_detect_active_channel_telegram(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TWILIO_ACCOUNT_SID", "")
        monkeypatch.setenv("TWILIO_AUTH_TOKEN", "")
        monkeypatch.setenv("TWILIO_WHATSAPP_NUMBER", "")
        import config as _root_config
        importlib.reload(_root_config)
        import ai_chatbot.config as _cfg
        importlib.reload(_cfg)
        import developer_alerts
        importlib.reload(developer_alerts)
        assert developer_alerts.detect_active_channel() == "telegram"

    def test_detect_active_channel_whatsapp(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
        monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
        monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
        monkeypatch.setenv("TWILIO_WHATSAPP_NUMBER", "whatsapp:+972000")
        import config as _root_config
        importlib.reload(_root_config)
        import ai_chatbot.config as _cfg
        importlib.reload(_cfg)
        import developer_alerts
        importlib.reload(developer_alerts)
        assert developer_alerts.detect_active_channel() == "whatsapp"

    def test_detect_dual_channel_returns_none(self, tmp_path, monkeypatch):
        """
        סביבת בדיקות שבה גם Telegram וגם Twilio מוגדרים — מוחזר None
        כדי שלא ייווצר false-positive mismatch ב-startup על כל חבילה.
        """
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
        monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
        monkeypatch.setenv("TWILIO_WHATSAPP_NUMBER", "whatsapp:+972000")
        import config as _root_config
        importlib.reload(_root_config)
        import ai_chatbot.config as _cfg
        importlib.reload(_cfg)
        import developer_alerts
        importlib.reload(developer_alerts)
        assert developer_alerts.detect_active_channel() is None

    def test_dual_channel_no_mismatch_alert(self, tmp_path, monkeypatch):
        """כששני הערוצים מוגדרים — לא נשלחת התראת mismatch לאף חבילה."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
        monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
        monkeypatch.setenv("TWILIO_WHATSAPP_NUMBER", "whatsapp:+972000")
        monkeypatch.setenv("DEVELOPER_TELEGRAM_CHAT_ID", "987654321")
        import config as _root_config
        importlib.reload(_root_config)
        import ai_chatbot.config as _cfg
        importlib.reload(_cfg)
        import database
        importlib.reload(database)
        database.init_db()
        import feature_flags
        importlib.reload(feature_flags)
        # premium = WhatsApp expected — היה "telegram" ב-detect הישן ⇒ mismatch
        feature_flags.set_plan("premium", reason="test dual channel")
        import developer_alerts
        importlib.reload(developer_alerts)

        sent_calls = []
        monkeypatch.setattr(
            developer_alerts, "_send_telegram",
            lambda *a, **k: sent_calls.append(a) or True,
        )

        summary = developer_alerts.check_and_alert_channel_mismatch()
        assert summary is not None
        assert summary["actual_channel"] is None  # dual-channel
        assert summary["is_mismatch"] is False
        assert summary["alert_sent"] is False
        assert sent_calls == []

    def test_check_alerts_on_mismatch(self, tmp_path, monkeypatch):
        # החבילה basic (ערוץ צפוי = telegram), אבל בפועל רק WhatsApp
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
        monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
        monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
        monkeypatch.setenv("TWILIO_WHATSAPP_NUMBER", "whatsapp:+972000")
        monkeypatch.setenv("DEVELOPER_TELEGRAM_CHAT_ID", "987654321")
        import config as _root_config
        importlib.reload(_root_config)
        import ai_chatbot.config as _cfg
        importlib.reload(_cfg)
        import database
        importlib.reload(database)
        database.init_db()
        import feature_flags
        importlib.reload(feature_flags)
        # ברירת המחדל premium — קובעים basic (ערוץ צפוי=telegram) ליצירת mismatch מול WhatsApp
        feature_flags.set_plan("basic", reason="test")
        assert feature_flags.get_current_plan() == "basic"
        import developer_alerts
        importlib.reload(developer_alerts)

        sent_calls = []

        def _fake_send(chat_id, text):
            sent_calls.append((chat_id, text))
            return True

        monkeypatch.setattr(developer_alerts, "_send_telegram", _fake_send)

        summary = developer_alerts.check_and_alert_channel_mismatch()
        assert summary is not None
        assert summary["plan"] == "basic"
        assert summary["expected_channel"] == "telegram"
        assert summary["actual_channel"] == "whatsapp"
        assert summary["is_mismatch"] is True
        assert summary["alert_sent"] is True
        # התראה הכילה את שני הערוצים
        assert "telegram" in sent_calls[0][1]
        assert "whatsapp" in sent_calls[0][1]

    def test_check_no_alert_on_match(self, tmp_path, monkeypatch):
        # החבילה basic (telegram) + רק TELEGRAM_BOT_TOKEN מוגדר → אין mismatch
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake")
        monkeypatch.setenv("TWILIO_ACCOUNT_SID", "")
        monkeypatch.setenv("TWILIO_AUTH_TOKEN", "")
        monkeypatch.setenv("TWILIO_WHATSAPP_NUMBER", "")
        monkeypatch.setenv("DEVELOPER_TELEGRAM_CHAT_ID", "987654321")
        import config as _root_config
        importlib.reload(_root_config)
        import ai_chatbot.config as _cfg
        importlib.reload(_cfg)
        import database
        importlib.reload(database)
        database.init_db()
        import feature_flags
        importlib.reload(feature_flags)
        # ברירת המחדל premium (ערוץ צפוי=whatsapp) — קובעים basic כדי שהערוץ
        # הצפוי=telegram יתאים ל-TELEGRAM_BOT_TOKEN המוגדר → אין mismatch.
        feature_flags.set_plan("basic", reason="test")
        import developer_alerts
        importlib.reload(developer_alerts)

        sent_calls = []
        monkeypatch.setattr(
            developer_alerts, "_send_telegram",
            lambda *a, **k: sent_calls.append(a) or True,
        )

        summary = developer_alerts.check_and_alert_channel_mismatch()
        assert summary["is_mismatch"] is False
        assert summary["alert_sent"] is False
        assert sent_calls == []


# ── page_type לעמוד fallback ──────────────────────────────────────────────


class TestPageType:
    def test_create_response_page_default_is_whatsapp_fallback(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
        import config as _root_config
        importlib.reload(_root_config)
        import ai_chatbot.config as _cfg
        importlib.reload(_cfg)
        import database
        importlib.reload(database)
        database.init_db()

        page_id = database.create_response_page("hello", title="x", user_id="u1")
        with database.get_connection() as conn:
            row = conn.execute(
                "SELECT page_type FROM response_pages WHERE id = ?", (page_id,)
            ).fetchone()
        assert row["page_type"] == "whatsapp_fallback"

    def test_create_response_page_explicit_landing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
        import config as _root_config
        importlib.reload(_root_config)
        import ai_chatbot.config as _cfg
        importlib.reload(_cfg)
        import database
        importlib.reload(database)
        database.init_db()

        page_id = database.create_response_page(
            "marketing content", title="x", user_id="", page_type="landing",
        )
        with database.get_connection() as conn:
            row = conn.execute(
                "SELECT page_type FROM response_pages WHERE id = ?", (page_id,)
            ).fetchone()
        assert row["page_type"] == "landing"
