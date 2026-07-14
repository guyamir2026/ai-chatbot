"""
טסטי ראוטינג ערוצים נכנס (multi-tenant שלב 2, spec פרק 6).

מכסים: ‏WhatsApp webhook פר-tenant (מפתח ב-URL + חתימה עם הטוקן הנכון),
‏resolve של Meta לפי entry.id מול ה-control plane, ‏widget עם מפתח,
עמודים ציבוריים תלויי-tenant ובוני ה-URL.
"""

from unittest.mock import patch

import pytest

import control_plane as cp
from tenancy import DEFAULT_TENANT, tenant_context


def _make_app():
    """יצירת אפליקציית אדמין לטסט — בלי תלות בסדר טעינת env של טסטים אחרים.

    admin/app.py מקפיא את קבועי ה-auth בייבוא; patch על אטריביוטי המודול
    מבטיח שה-boot guard עובר בכל סדר הרצה.
    """
    import admin.app as admin_app
    from unittest.mock import patch as _patch

    with _patch.object(admin_app, "ADMIN_SECRET_KEY", "test-secret"), \
         _patch.object(admin_app, "ADMIN_USERNAME", "admin"), \
         _patch.object(admin_app, "ADMIN_PASSWORD", "pw"):
        app = admin_app.create_admin_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture
def platform_env(tmp_path):
    """סביבת פלטפורמה מבודדת עם tenant אחד רשום ומאובזר."""
    with patch("ai_chatbot.config.DATA_DIR", tmp_path), \
         patch("ai_chatbot.config.DB_PATH", tmp_path / "default.db"):
        cp.invalidate_status_cache()
        # גם ה-DB של ה-tenant של ברירת המחדל מאותחל — הטסטים בודקים
        # הפרדה בינו לבין ה-tenants הרשומים
        from ai_chatbot import database as _db

        _db.init_db()
        cp.create_tenant("salon-a", "מספרת דנה")
        cp.set_tenant_secret("salon-a", "twilio_account_sid", "AC-a")
        cp.set_tenant_secret("salon-a", "twilio_auth_token", "tok-a")
        cp.set_tenant_secret("salon-a", "twilio_whatsapp_number", "+2000")
        cp.set_route("twilio_webhook_key", "wkey-a", "salon-a")
        cp.set_route("widget_key", "widk-a", "salon-a")
        yield tmp_path
        cp.invalidate_status_cache()


class TestWhatsappWebhookRouting:
    def test_resolve_webhook_tenant(self, platform_env):
        from messaging import whatsapp_webhook as wh

        assert wh._resolve_webhook_tenant(None) == DEFAULT_TENANT
        assert wh._resolve_webhook_tenant("wkey-a") == "salon-a"
        assert wh._resolve_webhook_tenant("no-such-key") is None

    def test_signature_validated_with_tenant_token(self, platform_env):
        """החתימה של tenant מאומתת עם הטוקן *שלו* — לא עם ה-env.

        ה-validator מזויף ודטרמיניסטי — הטסט בודק את הלוגיקה שלנו (איזה
        טוקן נבחר לפי ה-tenant), לא את הקריפטו של twilio. זה גם מנתק את
        הטסט מה-mock הגלובלי של מודול twilio שמתקין test_whatsapp_adapter
        בזמן ה-collection.
        """
        from flask import Flask
        from messaging import whatsapp_webhook as wh

        class FakeValidator:
            def __init__(self, token):
                self._token = token

            def validate(self, url, params, signature):
                return signature == f"sig-{self._token}"

        app = Flask(__name__)
        url = "https://x.test/webhook/whatsapp/t/wkey-a"
        form = {"From": "whatsapp:+972500000001", "Body": "hi"}

        with patch("twilio.request_validator.RequestValidator", FakeValidator), \
             app.test_request_context(
                 url, method="POST", data=form,
                 headers={"X-Twilio-Signature": "sig-tok-a"},
             ):
            with tenant_context("salon-a"):
                assert wh._validate_twilio_signature() is True
            # אותה בקשה תחת ה-tenant הלא-נכון (טוקן env אחר) — נכשלת
            with patch("ai_chatbot.config.TWILIO_ACCOUNT_SID", "AC-env"), \
                 patch("ai_chatbot.config.TWILIO_AUTH_TOKEN", "tok-env"), \
                 patch("ai_chatbot.config.TWILIO_WHATSAPP_NUMBER", "+1000"):
                assert wh._validate_twilio_signature() is False

    def test_unknown_key_returns_404(self, platform_env):
        client = _make_app().test_client()
        resp = client.post("/webhook/whatsapp/t/no-such-key", data={})
        assert resp.status_code == 404

    def test_tenant_route_processes_message_in_tenant_db(self, platform_env):
        """‏E2E: ‏POST חתום לנתיב של salon-a נשמר ב-DB של salon-a בלבד."""
        from ai_chatbot import database as db

        client = _make_app().test_client()

        path = "/webhook/whatsapp/t/wkey-a"
        form = {
            "From": "whatsapp:+972500000001",
            "To": "whatsapp:+2000",
            "Body": "שלום",
            "ProfileName": "Alice",
        }

        class FakeValidator:
            def __init__(self, token):
                self._token = token

            def validate(self, url, params, signature):
                return signature == f"sig-{self._token}"

        # עוצרים את הצינור אחרי שמירת ההודעה — לא מריצים RAG/LLM אמיתיים
        with patch("twilio.request_validator.RequestValidator", FakeValidator), \
             patch(
                 "core.message_processor.process_incoming_message",
                 return_value=None,
             ), patch(
                 "messaging.whatsapp_webhook._send_whatsapp_response"
             ):
            resp = client.post(
                path, data=form, headers={"X-Twilio-Signature": "sig-tok-a"},
            )

        assert resp.status_code == 200
        # המשתמש נכתב ל-DB של salon-a — כולל ה-To כ-provider_asset_id
        with tenant_context("salon-a"):
            with db.get_connection() as conn:
                row = conn.execute(
                    "SELECT provider_asset_id FROM users WHERE user_id = ?",
                    ("+972500000001",),
                ).fetchone()
        assert row is not None
        assert row["provider_asset_id"] == "+2000"
        # וב-DB של ברירת המחדל — אין זכר
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM users WHERE user_id = ?", ("+972500000001",),
            ).fetchone()
        assert row is None


class TestMetaEntryRouting:
    def test_control_plane_route_wins(self, platform_env):
        from messaging import meta_webhook as mw

        cp.set_route("meta_page_id", "page-123", "salon-a")
        assert mw._resolve_entry_tenant("page-123") == "salon-a"

    def test_ig_account_route(self, platform_env):
        from messaging import meta_webhook as mw

        cp.set_route("meta_ig_account", "ig-456", "salon-a")
        assert mw._resolve_entry_tenant("ig-456") == "salon-a"

    def test_fallback_to_default_local_credentials(self, platform_env):
        """entry שלא רשום ב-control plane אבל קיים ב-DB של default — legacy."""
        from messaging import meta_webhook as mw
        from ai_chatbot import database as db

        db.init_db()  # ה-DB של default ב-tmp
        db.upsert_meta_credentials(
            page_id="page-legacy", access_token="tok", page_name="p",
        )
        assert mw._resolve_entry_tenant("page-legacy") == DEFAULT_TENANT

    def test_unknown_entry_returns_none(self, platform_env):
        from messaging import meta_webhook as mw
        from ai_chatbot import database as db

        db.init_db()
        assert mw._resolve_entry_tenant("ghost-page") is None
        assert mw._resolve_entry_tenant(None) is None


class TestWidgetRouting:
    def test_resolver(self, platform_env):
        from admin import widget as w

        assert w._resolve_widget_tenant(None) == DEFAULT_TENANT
        assert w._resolve_widget_tenant("widk-a") == "salon-a"
        assert w._resolve_widget_tenant("nope") is None

    def test_chat_with_key_runs_in_tenant_context(self, platform_env):
        client = _make_app().test_client()

        seen = {}

        def fake_answer(**kwargs):
            from tenancy import get_current_tenant

            seen["tenant"] = get_current_tenant()
            return {"answer": "תשובה", "sources": []}

        with patch("admin.widget.generate_answer", side_effect=fake_answer):
            resp = client.post(
                "/widget/api/chat",
                json={"message": "שלום", "key": "widk-a"},
            )
        assert resp.status_code == 200
        assert seen["tenant"] == "salon-a"

    def test_chat_with_unknown_key_404(self, platform_env):
        client = _make_app().test_client()
        resp = client.post("/widget/api/chat", json={"message": "x", "key": "bad"})
        assert resp.status_code == 404

    def test_embed_js_with_key_bakes_key(self, platform_env):
        client = _make_app().test_client()
        resp = client.get("/widget/embed.js?k=widk-a")
        assert resp.status_code == 200
        assert b"widk-a" in resp.data
        # מפתח לא מוכר — 404
        assert client.get("/widget/embed.js?k=bad").status_code == 404


class TestPublicPages:
    def test_tenant_page_served_from_tenant_db(self, platform_env):
        from ai_chatbot import database as db

        with tenant_context("salon-a"):
            page_id = db.create_response_page(content="תוכן של א", title="עמוד")

        client = _make_app().test_client()

        resp = client.get(f"/t/salon-a/p/{page_id}")
        assert resp.status_code == 200
        assert "תוכן של א" in resp.get_data(as_text=True)
        # אותו page_id בנתיב ה-legacy (DB של default) — לא קיים
        assert client.get(f"/p/{page_id}").status_code == 404

    def test_suspended_tenant_page_404(self, platform_env):
        from ai_chatbot import database as db

        with tenant_context("salon-a"):
            page_id = db.create_response_page(content="x", title="t")
        cp.set_tenant_status("salon-a", "suspended")

        client = _make_app().test_client()
        assert client.get(f"/t/salon-a/p/{page_id}").status_code == 404

    def test_invalid_slug_404(self, platform_env):
        client = _make_app().test_client()
        assert client.get("/t/BAD SLUG/p/xyz").status_code == 404


class TestPublicUrlBuilders:
    def test_default_tenant_legacy_paths(self, platform_env):
        import public_urls as pu

        with patch("ai_chatbot.config.ADMIN_URL", "https://app.example.com"):
            assert pu.public_page_url("abc") == "https://app.example.com/p/abc"
            assert pu.public_ics_url("abc") == "https://app.example.com/ics/abc"
            assert pu.whatsapp_status_callback_url() == (
                "https://app.example.com/webhook/whatsapp/status"
            )

    def test_tenant_paths_include_slug_and_key(self, platform_env):
        import public_urls as pu

        with patch("ai_chatbot.config.ADMIN_URL", "https://app.example.com"):
            with tenant_context("salon-a"):
                assert pu.public_page_url("abc") == (
                    "https://app.example.com/t/salon-a/p/abc"
                )
                assert pu.whatsapp_status_callback_url() == (
                    "https://app.example.com/webhook/whatsapp/t/wkey-a/status"
                )

    def test_tenant_without_key_gets_no_callback(self, platform_env):
        import public_urls as pu

        cp.create_tenant("salon-b", "ב")
        with patch("ai_chatbot.config.ADMIN_URL", "https://app.example.com"):
            with tenant_context("salon-b"):
                assert pu.whatsapp_status_callback_url() is None
